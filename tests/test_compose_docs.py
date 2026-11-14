"""Stdlib-only static checks for the explicit Compose documentation subset.

No generic YAML parser is claimed. No Docker, shell, network or Secret IO occurs.
The doc reviewer additionally parses both YAML blocks with the available parser.
"""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs/DOCKER-COMPOSE-EXAMPLE.md'
ALL = 'ghcr.io/emengweb/restic-backup-docker@sha256:52c23fa4bd6fa439a4b8e358edc01113aa45337402452e90dff38a388b69f22f'
LITE = 'ghcr.io/emengweb/restic-backup-docker@sha256:16059ef22add33989b495593e754103b7daf6ecdafe5f14e423d4c97238000ed'
FENCE = chr(96) * 3


def marked_blocks(text, language, marker):
    result = {}
    for raw in text.split(FENCE + language + '\n')[1:]:
        body = raw.split('\n' + FENCE, 1)[0]
        first, _, remaining = body.partition('\n')
        if first.startswith(marker):
            result[first[len(marker):]] = remaining
    return result


def section(text, name):
    lines = text.splitlines()
    start = lines.index(name + ':') + 1
    selected = []
    for line in lines[start:]:
        if line and not line.startswith(' '):
            break
        selected.append(line)
    return '\n'.join(selected) + '\n'


def services(text):
    result = {}
    current = None
    for line in section(text, 'services').splitlines():
        if line.startswith('  ') and not line.startswith('   ') and line.endswith(':'):
            current = line.strip()[:-1]
            result[current] = []
        elif current:
            result[current].append(line)
    return {name: '\n'.join(lines) for name, lines in result.items()}


def field(body, name):
    prefix = '    ' + name + ': '
    return next((line[len(prefix):] for line in body.splitlines() if line.startswith(prefix)), None)


def list_field(body, name):
    lines = body.splitlines()
    header = '    ' + name + ':'
    if header not in lines:
        return []
    result = []
    for line in lines[lines.index(header) + 1:]:
        if line and not line.startswith('      '):
            break
        if line.startswith('      - '):
            result.append(line[len('      - '):])
    return result


class ComposeDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding='utf-8')
        cls.configs = marked_blocks(cls.text, 'yaml', '# compose-example: ')

    def test_two_complete_service_graphs(self):
        self.assertEqual(set(self.configs), {'files', 'mariadb'})
        self.assertEqual(set(services(self.configs['files'])), {'restore-init', 'app', 'backup'})
        self.assertEqual(set(services(self.configs['mariadb'])), {'restore-init', 'mariadb', 'backup'})

    def test_inherited_entrypoint_receives_only_subcommand(self):
        docker = (ROOT / 'Dockerfile').read_text().splitlines()
        raw = next(line[len('ENTRYPOINT '):] for line in docker if line.startswith('ENTRYPOINT '))
        entrypoint = json.loads(raw)
        self.assertEqual(entrypoint, ['python3', '-m', 'k3s_backup'])
        for text in self.configs.values():
            graph = services(text)
            for service, command in [('backup', 'schedule'), ('restore-init', 'restore-init')]:
                value = json.loads(field(graph[service], 'command'))
                self.assertEqual(value, [command])
                self.assertEqual(entrypoint + value, ['python3', '-m', 'k3s_backup', command])
                self.assertIsNone(field(graph[service], 'entrypoint'))
        self.assertIn('run --rm --no-deps backup health', self.text)
        self.assertFalse(any('docker compose' in line and 'run ' in line and 'backup python3' in line
                             for line in self.text.splitlines()))

    def test_fixed_digest_shared_by_restore_and_backup(self):
        for profile, digest, anchor in [('files', LITE, 'lite-image'), ('mariadb', ALL, 'all-image')]:
            text = self.configs[profile]
            self.assertIn('&' + anchor + ' ' + digest, text)
            graph = services(text)
            self.assertEqual(field(graph['backup'], 'image'), '*' + anchor)
            self.assertEqual(field(graph['restore-init'], 'image'), '*' + anchor)

    def test_restore_blocks_primary_and_no_dependency_cycle(self):
        for profile, primary in [('files', 'app'), ('mariadb', 'mariadb')]:
            graph = services(self.configs[profile])
            self.assertIn('restore-init: {condition: service_completed_successfully}', graph[primary])
            self.assertNotIn('depends_on:', graph['restore-init'])
            self.assertNotIn('profiles:', graph['restore-init'])
            self.assertEqual(field(graph['restore-init'], 'restart'), '"no"')
            self.assertEqual(field(graph[primary], 'restart'), '"no"')
        self.assertIn('mariadb: {condition: service_healthy}', services(self.configs['mariadb'])['backup'])

    def test_same_data_and_independent_scratch_mounts(self):
        for profile, data in [('files', 'data'), ('mariadb', 'db-data')]:
            text = self.configs[profile]
            graph = services(text)
            self.assertIn(data + ':/data', list_field(graph['restore-init'], 'volumes'))
            self.assertIn('restore-work:/backup', list_field(graph['restore-init'], 'volumes'))
            self.assertIn('backup-work:/backup', list_field(graph['backup'], 'volumes'))
            self.assertIn('external: true', section(text, 'volumes'))
        self.assertIn('data:/data:ro', list_field(services(self.configs['files'])['backup'], 'volumes'))
        maria = services(self.configs['mariadb'])
        self.assertIn('db-data:/var/lib/mysql', list_field(maria['mariadb'], 'volumes'))
        self.assertNotIn('db-data', '\n'.join(list_field(maria['backup'], 'volumes')))

    def test_secret_volume_service_references_exist(self):
        for text in self.configs.values():
            declared_secrets = {line.strip().split(':', 1)[0] for line in section(text, 'secrets').splitlines()
                                if line.startswith('  ') and not line.startswith('   ')}
            declared_volumes = {line.strip().split(':', 1)[0] for line in section(text, 'volumes').splitlines()
                                if line.startswith('  ') and not line.startswith('   ')}
            graph = services(text)
            for body in graph.values():
                raw = field(body, 'secrets')
                if raw:
                    self.assertLessEqual({x.strip() for x in raw.strip('[]').split(',')}, declared_secrets)
                for mount in list_field(body, 'volumes'):
                    self.assertIn(mount.split(':', 1)[0], declared_volumes)
                for line in body.splitlines():
                    if line.startswith('      ') and ': {condition:' in line:
                        self.assertIn(line.strip().split(':', 1)[0], graph)

    def test_secrets_are_files_not_inline_values(self):
        for text in self.configs.values():
            self.assertIn('RESTIC_PASSWORD_FILE: /run/secrets/restic-password', text)
            self.assertIn('AWS_ACCESS_KEY_ID_FILE: /run/secrets/s3-access-key', text)
            self.assertIn('AWS_SECRET_ACCESS_KEY_FILE: /run/secrets/s3-secret-key', text)
            for line in text.splitlines():
                self.assertNotIn(line.strip().split(':', 1)[0],
                                 {'RESTIC_PASSWORD', 'DATABASE_PASSWORD', 'MARIADB_ROOT_PASSWORD'})
        self.assertNotIn('REPLACE_WITH_EXISTING_RESTIC_PASSWORD', self.text)
        self.assertIn('read -r -s', self.text)
        self.assertIn('noclobber', self.text)

    def test_maria_globals_admin_socket_not_app_only_account(self):
        text = self.configs['mariadb']
        self.assertIn('DATABASE_USER: root', text)
        self.assertIn('DATABASE_PASSWORD_FILE: /run/secrets/db-root-password', text)
        self.assertIn('MARIADB_SOCKET: /run/mysqld/mysqld.sock', text)
        self.assertIn('RESTORE_TARGET_MAJOR: "11.8"', text)
        self.assertIn('BACKUP_MODE: logical', text)
        self.assertIn('DB_UID: ' + '$' + '{DB_UID:?', text)
        self.assertIn('DB_GID: ' + '$' + '{DB_GID:?', text)
        self.assertIn('db-socket:/run/mysqld:ro', list_field(services(text)['backup'], 'volumes'))
        primary = services(text)['mariadb']
        for item in ('MARIADB_ROOT_HOST', 'MARIADB_ALLOW_EMPTY', 'MARIADB_DATABASE:', 'ports:'):
            self.assertNotIn(item, primary)
        self.assertIn('GRANT PROXY', self.text)
        self.assertIn('不宣称最小权限', self.text)

    def test_maria_reviewed_main_image_and_health_secret_not_argv(self):
        primary = services(self.configs['mariadb'])['mariadb']
        self.assertEqual(field(primary, 'image'), '$' + '{MARIADB_IMAGE:?set_reviewed_mariadb_11_8_9_digest}')
        raw = next(line.strip()[len('test: '):] for line in primary.splitlines() if line.strip().startswith('test: '))
        probe = json.loads(raw)
        self.assertEqual(probe[0], 'CMD-SHELL')
        self.assertIn('$' * 2 + '(cat /run/secrets/db-root-password)', probe[1])
        self.assertIn('MYSQL_PWD=', probe[1])
        self.assertNotIn('--password', probe[1])
        self.assertIn('SELECT 1', probe[1])

    def test_restore_operations_stop_on_error_before_up(self):
        blocks = marked_blocks(self.text, 'bash', '# compose-operation: ')
        self.assertEqual(set(blocks), {'files', 'mariadb'})
        for profile, block in blocks.items():
            self.assertIn('set -euo pipefail', block)
            self.assertLess(block.index(' stop '), block.index(' run --rm --no-deps restore-init'))
            self.assertLess(block.index(' run --rm --no-deps restore-init'), block.index(' up -d '))
            self.assertIn('compose.' + profile + '.yaml', block)
            self.assertNotIn('down -v', block)

    def test_honest_execution_and_compatibility_limits(self):
        for text in ('不能阻止其他 Compose project', '非空', '没有运行 docker compose config',
                     '超过默认7天', '不允许11.8打开旧11.5物理卷', 'MySQL', '最小权限'):
            self.assertIn(text, self.text)
        readme = (ROOT / 'README.md').read_text()
        self.assertIn('command: ["schedule"]', readme)
        self.assertIn('docs/DOCKER-COMPOSE-EXAMPLE.md', readme)
        self.assertIn('不是一次实际Compose部署', readme)


if __name__ == '__main__':
    unittest.main()
