"""Synthetic filesystem/ldd parser tests. No Docker, real data or host fallback."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('runtime_closure', Path(__file__).parents[1] / 'scripts/collect-runtime.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ParserTests(unittest.TestCase):
    def test_dynamic_loader_and_libraries(self):
        value = ' linux-vdso.so.1 (0x123)\n libssl.so.3 => /lib/a/libssl.so.3 (0x2)\n /lib64/ld-linux.so.2 (0x3)\n'
        self.assertEqual(module.parse_ldd(value), ['/lib/a/libssl.so.3', '/lib64/ld-linux.so.2'])

    def test_missing_dependency_is_fatal(self):
        with self.assertRaisesRegex(module.ClosureError, 'unresolved_elf_dependency'):
            module.parse_ldd('libssl.so.3 => not found\n')

    def test_static_elf_is_allowed(self):
        self.assertEqual(module.parse_ldd('not a dynamic executable\n', 1), [])

    def test_other_ldd_failure_is_fatal(self):
        with self.assertRaisesRegex(module.ClosureError, 'ldd_failed'):
            module.parse_ldd('unexpected failure', 2)

    def test_explicit_exclusions_not_database_servers(self):
        for path in ('/usr/lib/llvmjit.so', '/usr/lib/libLLVM.so.19', '/usr/lib/libgalera_smm.so',
                     '/usr/lib/postgresql/17/lib/bitcode/foo.bc', '/usr/lib/python3.11/__pycache__/x.pyc'):
            self.assertTrue(module.excluded(path))
        for path in ('/usr/lib/postgresql/17/bin/postgres', '/usr/sbin/mariadbd',
                     '/usr/lib/postgresql/17/lib/vector.so', '/usr/lib/python3.11/lib-dynload/_ssl.so'):
            self.assertFalse(module.excluded(path))

    def test_shell_and_native_servers_are_required(self):
        self.assertIn('/usr/sbin/mariadbd', module.TOOLS)
        self.assertIn('/usr/bin/mariadb-backup', module.TOOLS)
        self.assertIn('/bin/sh', module.TOOLS)
        for tool in ('postgres', 'initdb', 'pg_basebackup', 'pg_verifybackup', 'pg_dump', 'pg_restore'):
            self.assertIn(tool, module.PG_TOOLS)


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.output = self.base / 'output'
        self.c = module.Collector(self.source, ldd=lambda path, root: [])

    def file(self, path, data=b'fixture', mode=0o640):
        p = self.source / path.lstrip('/')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(mode)
        return p

    def test_usrmerge_preserves_links_modes_ownership(self):
        source = self.file('/usr/bin/tool', mode=0o751)
        (self.source / 'bin').symlink_to('usr/bin')
        self.c.add('/bin/tool')
        self.c.materialize(self.output)
        self.assertEqual(os.readlink(self.output / 'bin'), 'usr/bin')
        dest = self.output / 'usr/bin/tool'
        self.assertEqual(dest.read_bytes(), b'fixture')
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o751)
        self.assertEqual((dest.stat().st_uid, dest.stat().st_gid), (source.stat().st_uid, source.stat().st_gid))
        inventory = json.loads((self.output / 'usr/share/k3s-restic/runtime-files.json').read_text())
        entry = next(e for e in inventory['files'] if e['path'] == '/usr/bin/tool')
        self.assertEqual(len(entry['sha256']), 64)
        self.assertEqual(entry['bytes'], 7)
        self.assertNotIn('/usr/share/k3s-restic/runtime-files.json', [e['path'] for e in inventory['files']])

    def test_generated_snakeoil_and_hash_alias_excluded_but_CA_preserved(self):
        self.file("/etc/ssl/certs/ssl-cert-snakeoil.pem", b"synthetic-random-build-certificate")
        self.file("/etc/ssl/certs/fixture-CA.pem", b"synthetic-trusted-ca")
        (self.source / "etc/ssl/certs/a1b2c3d4.0").symlink_to("ssl-cert-snakeoil.pem")
        (self.source / "etc/ssl/certs/f00dcafe.0").symlink_to("fixture-CA.pem")
        self.c.add("/etc/ssl/certs", tree=True)
        self.c.materialize(self.output)
        self.assertFalse((self.output / "etc/ssl/certs/ssl-cert-snakeoil.pem").exists())
        self.assertFalse((self.output / "etc/ssl/certs/a1b2c3d4.0").is_symlink())
        self.assertEqual((self.output / "etc/ssl/certs/fixture-CA.pem").read_bytes(), b"synthetic-trusted-ca")
        self.assertTrue((self.output / "etc/ssl/certs/f00dcafe.0").is_symlink())

    def test_independent_build_random_certificates_do_not_change_runtime_inventory(self):
        inventories = []
        for suffix in ["one", "two"]:
            source = self.base / suffix; source.mkdir()
            certs = source / "etc/ssl/certs"; certs.mkdir(parents=True)
            (certs / "ssl-cert-snakeoil.pem").write_text("synthetic-random-" + suffix)
            (certs / (suffix + ".0")).symlink_to("ssl-cert-snakeoil.pem")
            (certs / "fixture-CA.pem").write_text("synthetic-stable-ca")
            c = module.Collector(source, ldd=lambda p,r: [])
            c.add("/etc/ssl/certs", tree=True)
            out = self.base / (suffix + "-out"); c.materialize(out)
            inventories.append(json.loads((out / "usr/share/k3s-restic/runtime-files.json").read_text()))
        self.assertEqual(inventories[0], inventories[1])

    def test_absolute_symlink_stays_inside_source(self):
        self.file('/usr/lib/libfoo.so', b'source-not-host')
        (self.source / 'lib').symlink_to('/usr/lib')
        self.c.add('/lib/libfoo.so')
        self.c.materialize(self.output)
        self.assertTrue((self.output / 'lib').is_symlink())
        self.assertEqual((self.output / 'usr/lib/libfoo.so').read_bytes(), b'source-not-host')

    def test_missing_absolute_link_cannot_fall_back_to_host(self):
        (self.source / 'leak').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(module.ClosureError, 'required_path_missing'):
            self.c.add('/leak')

    def test_relative_link_escape_rejected(self):
        (self.source / 'escape').symlink_to('../../etc/passwd')
        with self.assertRaisesRegex(module.ClosureError, 'symlink_escapes_root'):
            self.c.add('/escape')

    def test_symlink_loop_rejected(self):
        (self.source / 'a').symlink_to('b')
        (self.source / 'b').symlink_to('a')
        with self.assertRaisesRegex(module.ClosureError, 'symlink_loop'):
            self.c.add('/a')

    def test_nonempty_output_preserved(self):
        self.file('/file')
        self.c.add('/file')
        self.output.mkdir()
        existing = self.output / 'keep'
        existing.write_bytes(b'keep')
        with self.assertRaisesRegex(module.ClosureError, 'output_not_empty'):
            self.c.materialize(self.output)
        self.assertEqual(existing.read_bytes(), b'keep')

    def test_symlink_output_parent_rejected(self):
        real = self.base / 'real'
        real.mkdir()
        link = self.base / 'link'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(module.ClosureError, 'symlink_output_forbidden'):
            self.c.materialize(link / 'out')

    def test_recursive_elf_dependencies(self):
        self.file('/usr/bin/app', b'\x7fELF')
        self.file('/usr/lib/libone.so', b'\x7fELF')
        self.file('/usr/lib/libtwo.so', b'\x7fELF')
        calls = []
        def ldd(path, root):
            logical = '/' + str(path.relative_to(root))
            calls.append(logical)
            return {'/usr/bin/app': ['/usr/lib/libone.so'], '/usr/lib/libone.so': ['/usr/lib/libtwo.so']}.get(logical, [])
        self.c.ldd = ldd
        self.c.add('/usr/bin/app')
        self.c.close_elfs()
        self.assertEqual(set(calls), {'/usr/bin/app', '/usr/lib/libone.so', '/usr/lib/libtwo.so'})

    def test_excluded_elf_dependency_cannot_silently_disappear(self):
        self.file('/usr/bin/app', b'\x7fELF')
        self.c.ldd = lambda path, root: ['/usr/lib/libLLVM.so.19']
        self.c.add('/usr/bin/app')
        with self.assertRaisesRegex(module.ClosureError, 'excluded_dependency_required'):
            self.c.close_elfs()

    def test_required_glob_is_fatal(self):
        with self.assertRaisesRegex(module.ClosureError, 'required_pattern_empty'):
            self.c.pattern('/usr/lib/*/required.so', required=True)

    def test_regular_data_shebang_is_not_executed(self):
        self.file('/usr/lib/python3.11/data.py', b'#!/nonexistent/python\n')
        self.c.add('/usr/lib/python3.11/data.py')
        self.c.close_elfs()
        self.assertNotIn('/nonexistent/python', self.c.nodes)

    def test_real_tool_interpreter_is_required(self):
        self.file('/usr/bin/tool', b'#!/bin/sh\n')
        self.file('/usr/bin/dash', b'fixture')
        (self.source / 'bin').symlink_to('usr/bin')
        (self.source / 'usr/bin/sh').symlink_to('dash')
        self.c.add('/usr/bin/tool')
        self.c.close_elfs()
        self.assertIn('/usr/bin/dash', self.c.nodes)

    def test_special_files_rejected(self):
        os.mkfifo(self.source / 'fifo')
        with self.assertRaisesRegex(module.ClosureError, 'special_file_forbidden'):
            self.c.add('/fifo')

    def test_no_chroot_ldd_host_fallback(self):
        with self.assertRaisesRegex(module.ClosureError, 'ldd_non_native_root_forbidden'):
            module.native_ldd(self.source / 'fixture', self.source)

    def test_slim_marker_is_truthful_and_self_excludes(self):
        self.file('/runtime')
        self.c.add('/runtime')
        self.c.materialize(self.output)
        marker = json.loads((self.output / 'usr/share/k3s-restic/slim-runtime.json').read_text())
        self.assertEqual(marker['excluded_features'], ['postgres_jit', 'galera'])
        self.assertEqual(marker['runtime_file_count'], len(self.c.nodes))
        self.assertIn('partially', marker['package_inventory_scope'])


class ProfileTests(unittest.TestCase):
    def test_capability_shapes_and_default_all(self):
        self.assertEqual(module.PROFILES, ('files', 'lite', 'all'))
        expected = {'files': ([], []), 'lite': ([], []),
                    'all': (['16', '17'], ['11.8'])}
        for profile, (pg, maria) in expected.items():
            cap = module.profile_capabilities(profile)
            self.assertEqual(cap['profile'], profile)
            self.assertEqual(cap['postgres_majors'], pg)
            self.assertEqual(cap['mariadb_series'], maria)
            self.assertIs(cap['files'], True)
        for unsupported in ('pg16', 'pg17', 'postgres', 'mariadb11.8', 'pg18'):
            with self.assertRaisesRegex(module.ClosureError, 'unknown_profile'):
                module.profile_capabilities(unsupported)

    def test_optional_mariadb_pam_is_not_a_shared_files_dependency(self):
        for profile in ('files', 'lite'):
            self.assertTrue(module.profile_excluded('/usr/lib/a/security/pam_user_map.so', profile))
            self.assertTrue(module.profile_excluded('/etc/security/user_map.conf', profile))
            self.assertFalse(module.profile_excluded('/usr/lib/a/security/pam_unix.so', profile))
        self.assertFalse(module.profile_excluded('/usr/lib/a/security/pam_user_map.so', 'all'))

    def test_files_and_lite_cannot_be_their_own_delta_base(self):
        for profile in ('files', 'lite'):
            with self.assertRaisesRegex(module.ClosureError, 'profile_equals_base'):
                module.main(['--profile', profile, '--base-profile', 'files', '--dry-run'])

    def test_only_all_can_use_shared_files_delta(self):
        import contextlib
        import io
        for unsupported in ('pg16', 'pg17', 'postgres', 'mariadb11.8'):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    module.main(['--profile', unsupported, '--dry-run'])
            self.assertEqual(error.exception.code, 2)

    def test_common_tools_have_no_database_helpers(self):
        self.assertFalse(any('mariadb' in p or 'postgresql' in p for p in module.COMMON_TOOLS))
        self.assertIn('/usr/bin/mariadb-backup', module.MARIA_TOOLS)
        self.assertIn('/usr/sbin/mariadbd', module.MARIA_TOOLS)
        self.assertEqual(set(module.COMMON_TOOLS) | set(module.MARIA_TOOLS), set(module.TOOLS))

    def test_foreign_engine_path_is_rejected(self):
        class Fake:
            profile = 'files'
            nodes = {'/usr/lib/postgresql/17/bin/postgres': None}
        with self.assertRaisesRegex(module.ClosureError, 'foreign_postgres_resource'):
            module.validate_profile_paths(Fake())
        Fake.profile = 'lite'
        with self.assertRaisesRegex(module.ClosureError, 'foreign_postgres_resource'):
            module.validate_profile_paths(Fake())
        Fake.nodes = {'/usr/share/mariadb/mariadb_system_tables.sql': None}
        with self.assertRaisesRegex(module.ClosureError, 'mariadb_resource'):
            module.validate_profile_paths(Fake())

    def test_all_has_both_postgres_majors_and_mariadb_lite_has_none(self):
        class Recording:
            def __init__(self):
                self.paths, self.nodes, self.empty_dirs = [], {}, {}
            def add(self, path, **kwargs): self.paths.append(path)
            def maybe(self, path, **kwargs): self.paths.append(path)
            def pattern(self, path, **kwargs): self.paths.append(path)
            def source(self, path): return Path(path)
            def close_elfs(self): pass
            def add_package_licenses(self): pass
        for profile in module.PROFILES:
            c = module.runtime_plan(Recording(), profile)
            joined = '\n'.join(c.paths)
            cap = module.profile_capabilities(profile)
            for major in ('16', '17'):
                self.assertEqual('/usr/lib/postgresql/' + major + '/bin/postgres' in c.paths,
                                 major in cap['postgres_majors'])
            self.assertEqual('/usr/share/mariadb' in c.paths, bool(cap['mariadb_series']))
            self.assertEqual('/usr/lib/mysql/plugin' in c.paths, bool(cap['mariadb_series']))
            self.assertEqual('/usr/sbin/mariadbd' in c.paths, bool(cap['mariadb_series']))
            self.assertEqual('/var/lib/postgresql' in c.empty_dirs, bool(cap['postgres_majors']))
            self.assertEqual('/var/lib/mysql' in c.empty_dirs, bool(cap['mariadb_series']))


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base_source = self.root / 'base-source'
        self.full_source = self.root / 'full-source'
        self.base_source.mkdir(); self.full_source.mkdir()
        self.base = module.Collector(self.base_source, ldd=lambda p, r: [])
        self.full = module.Collector(self.full_source, ldd=lambda p, r: [])
        self.base.profile, self.full.profile = 'files', 'all'

    def file(self, c, path, data=b'same', mode=0o644):
        p = c.source(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data); p.chmod(mode)
        c.add(path)

    def test_identical_files_not_recopied_and_base_untouched(self):
        for c in (self.base, self.full): self.file(c, '/usr/lib/shared.so')
        self.file(self.full, '/usr/lib/postgresql/16/bin/postgres', b'new', 0o755)
        before = self.base.signature('/usr/lib/shared.so')
        selected = self.full.delta_nodes(self.base)
        self.assertNotIn('/usr/lib/shared.so', selected)
        self.assertIn('/usr/lib/postgresql/16/bin/postgres', selected)
        self.assertIn('/usr/lib', selected)
        delta = self.root / 'delta'
        self.full.materialize(delta, self.base)
        self.assertFalse((delta / 'usr/lib/shared.so').exists())
        self.assertEqual((delta / 'usr/lib/postgresql/16/bin/postgres').read_bytes(), b'new')
        self.assertEqual(self.base.signature('/usr/lib/shared.so'), before)
        cap = json.loads((delta / 'usr/share/k3s-restic/profile.json').read_text())
        self.assertEqual(cap['profile'], 'all')
        manifest = json.loads((delta / 'usr/share/k3s-restic/runtime-files.json').read_text())
        self.assertEqual(manifest['base_profile'], 'files')
        self.assertIn('/usr/lib/shared.so', [p['path'] for p in manifest['files']])

    def test_same_length_different_bytes_are_not_shared(self):
        self.file(self.base, '/file', b'abcd')
        self.file(self.full, '/file', b'efgh')
        self.assertIn('/file', self.full.delta_nodes(self.base))

    def test_mode_and_uid_gid_differences_are_not_shared(self):
        self.file(self.base, '/file', mode=0o644)
        self.file(self.full, '/file', mode=0o600)
        self.assertIn('/file', self.full.delta_nodes(self.base))
        self.full.source('/file').chmod(0o644)
        self.full.nodes['/file'] = (self.full.source('/file').stat(), None)
        from types import SimpleNamespace
        info = self.full.nodes['/file'][0]
        self.full.nodes['/file'] = (SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1,
                                                  st_gid=info.st_gid, st_size=info.st_size), None)
        self.assertIn('/file', self.full.delta_nodes(self.base))

    def test_same_symlink_is_inherited_not_materialized_as_directory(self):
        for c in (self.base, self.full):
            self.file(c, '/usr/bin/sh')
            (c.root / 'bin').symlink_to('usr/bin')
            c.add('/bin/sh')
        self.file(self.full, '/usr/bin/dbtool', b'new')
        selected = self.full.delta_nodes(self.base)
        self.assertNotIn('/bin', selected)
        delta = self.root / 'delta'
        self.full.materialize(delta, self.base)
        self.assertFalse((delta / 'bin').exists())
        self.assertTrue((delta / 'usr/bin/dbtool').is_file())

    def test_symlink_target_difference_included(self):
        for c in (self.base, self.full):
            self.file(c, '/one'); self.file(c, '/two')
        (self.base.root / 'link').symlink_to('one')
        (self.full.root / 'link').symlink_to('two')
        self.base.add('/link'); self.full.add('/link')
        self.assertIn('/link', self.full.delta_nodes(self.base))

    def test_inherited_empty_directories_not_in_delta(self):
        self.file(self.base, '/usr/bin/tool'); self.file(self.full, '/usr/bin/tool')
        self.base.empty_dirs['/backup'] = self.full.empty_dirs['/backup'] = (0o700, os.getuid(), os.getgid())
        self.full.empty_dirs['/var/lib/postgresql'] = (0o700, os.getuid(), os.getgid())
        delta = self.root / 'delta'
        self.full.materialize(delta, self.base)
        self.assertFalse((delta / 'backup').exists())
        self.assertTrue((delta / 'var/lib/postgresql').is_dir())


if __name__ == '__main__':
    unittest.main()
