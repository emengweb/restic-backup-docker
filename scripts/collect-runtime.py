#!/usr/bin/env python3
"""Explicit trusted build-stage runtime closure. No network or package installer."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess


class ClosureError(Exception):
    pass


def logical(value):
    value = str(value)
    if not value.startswith('/') or '\0' in value or '..' in PurePosixPath(value).parts:
        raise ClosureError('invalid_absolute_path')
    return posixpath.normpath(value)


def parse_ldd(text, returncode=0):
    if re.search(r'=>\s+not found(?:\s|$)', text):
        raise ClosureError('unresolved_elf_dependency')
    if returncode and not any(x in text for x in ('not a dynamic executable', 'statically linked')):
        raise ClosureError('ldd_failed')
    result = set()
    for line in text.splitlines():
        m = re.match(r'\s*(?:\S+\s+=>\s+)?(/[^\s()]+)(?:\s|\()', line)
        if m:
            result.add(logical(m.group(1)))
    return sorted(result)


def native_ldd(path, root):
    if root != Path('/'):
        raise ClosureError('ldd_non_native_root_forbidden')
    try:
        r = subprocess.run(['/usr/bin/ldd', str(path)], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=20,
                           env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        raise ClosureError('ldd_unavailable_or_timeout') from None
    return parse_ldd(r.stdout.decode('utf-8', 'replace'), r.returncode)


def excluded(path):
    p = PurePosixPath(path)
    return (any(x in {'__pycache__', 'bitcode', 'pgxs'} for x in p.parts)
            or p.name.endswith(('.pyc', '.pyo', '.a', '.bc'))
            or p.name.startswith(('llvmjit', 'libLLVM', 'libz3', 'libgalera'))
            or path in {'/etc/ssl/certs/ssl-cert-snakeoil.pem', '/etc/ssl/private/ssl-cert-snakeoil.key'}
            or path.startswith('/usr/lib/python3.11/test/'))


def profile_excluded(path, profile):
    if profile != 'all' and (path.endswith('/pam_user_map.so')
            or path == '/etc/security/user_map.conf'):
        return True
    return False


class Collector:
    def __init__(self, source_root='/', ldd=native_ldd):
        self.root = Path(source_root).absolute()
        if not self.root.is_dir() or self.root.is_symlink():
            raise ClosureError('invalid_source_root')
        self.nodes, self.trees, self.checked, self.elfs = {}, set(), set(), set()
        self.ldd, self.empty_dirs = ldd, {}
        self.packages = {}
        self.builder_inventory = None
        self.profile = 'all'
        self._fingerprints = {}

    def source(self, path):
        return self.root.joinpath(*PurePosixPath(logical(path)).parts[1:])

    def resolve(self, path):
        # Interpret absolute symlinks inside source_root, never on the host.
        pending = list(PurePosixPath(logical(path)).parts[1:])
        done, links, count = [], [], 0
        while pending:
            part = pending.pop(0)
            if part in ('', '.'):
                continue
            if part == '..':
                if not done:
                    raise ClosureError('symlink_escapes_root')
                done.pop()
                continue
            candidate = '/' + '/'.join(done + [part])
            try:
                info = self.source(candidate).lstat()
            except OSError:
                # Trusted build-stage program paths only; never inspect credentials.
                if candidate.startswith(('/usr/', '/lib/', '/bin/', '/sbin/', '/etc/')):
                    print(json.dumps({'diagnostic': 'missing_build_resource', 'path': candidate}),
                          file=__import__('sys').stderr)
                raise ClosureError('required_path_missing') from None
            if stat.S_ISLNK(info.st_mode):
                count += 1
                if count > 40:
                    raise ClosureError('symlink_loop')
                target = os.readlink(self.source(candidate))
                if not target or '\0' in target:
                    raise ClosureError('invalid_symlink')
                links.append((candidate, info, target))
                if target.startswith('/'):
                    done = []
                pending = target.split('/') + pending
            else:
                done.append(part)
        return '/' + '/'.join(done), links

    def record(self, path, info, target=None):
        if excluded(path):
            raise ClosureError('excluded_dependency_required')
        self.nodes[path] = (info, target)
        parent = str(PurePosixPath(path).parent)
        if parent != path and parent not in self.nodes:
            self.record(parent, self.source(parent).lstat())

    def add(self, path, *, tree=False):
        path = logical(path)
        if excluded(path) or profile_excluded(path, self.profile):
            return
        resolved, links = self.resolve(path)
        # Drop aliases to generated build-only material before recording any link.
        # Hashed snakeoil symlink names vary per builder; CA symlinks are retained.
        if excluded(resolved) or profile_excluded(resolved, self.profile):
            return
        for link, info, target in links:
            self.record(link, info, target)
        info = self.source(resolved).lstat()
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
            raise ClosureError('special_file_forbidden')
        self.record(resolved, info)
        if tree and stat.S_ISDIR(info.st_mode) and resolved not in self.trees:
            self.trees.add(resolved)
            for child in sorted(self.source(resolved).iterdir()):
                if not excluded(resolved + '/' + child.name):
                    self.add(resolved + '/' + child.name, tree=True)

    def maybe(self, path, *, tree=False):
        try:
            self.resolve(path)
        except ClosureError as error:
            if str(error) == 'required_path_missing':
                return False
            raise
        self.add(path, tree=tree)
        return True

    def pattern(self, pattern, *, required=False, tree=False):
        paths = sorted(self.root.glob(pattern.lstrip('/')))
        if required and not paths:
            raise ClosureError('required_pattern_empty')
        for path in paths:
            self.add('/' + str(path.relative_to(self.root)), tree=tree)
        return len(paths)

    def close_elfs(self):
        while True:
            pending = [p for p, (s, _) in self.nodes.items()
                       if stat.S_ISREG(s.st_mode) and p not in self.checked]
            if not pending:
                break
            for path in pending:
                self.checked.add(path)
                with self.source(path).open('rb') as stream:
                    magic = stream.read(4)
                    if magic == b'\x7fELF':
                        self.elfs.add(path)
                        for dependency in self.ldd(self.source(path), self.root):
                            if excluded(dependency):
                                raise ClosureError('excluded_dependency_required')
                            self.add(dependency)
                    elif magic.startswith(b'#!') and any(path.startswith(p) for p in
                            ('/usr/bin/', '/usr/sbin/', '/usr/local/bin/', '/usr/local/sbin/')):
                        stream.seek(0)
                        parts = stream.readline(4096)[2:].strip().split()
                        if not parts:
                            raise ClosureError('invalid_script_interpreter')
                        self.add(parts[0].decode('ascii'))

    def add_package_licenses(self):
        if self.root != Path('/'):
            raise ClosureError('package_mapping_non_native_root')
        paths = sorted(p for p, (s, _) in self.nodes.items() if not stat.S_ISDIR(s.st_mode))
        aliases = {}
        for path in paths:
            if path in {'/bin', '/sbin', '/lib', '/lib64'}:
                continue  # shared usrmerge dirs may be claimed by unrelated packages
            aliases[path] = path
            if path.startswith('/usr/lib/'):
                aliases['/lib/' + path[len('/usr/lib/'):]] = path
            elif path.startswith('/usr/bin/'):
                aliases['/bin/' + path[len('/usr/bin/'):]] = path
            elif path.startswith('/usr/sbin/'):
                aliases['/sbin/' + path[len('/usr/sbin/'):]] = path
        queries = sorted(aliases)
        for offset in range(0, len(queries), 120):
            r = subprocess.run(['/usr/bin/dpkg-query', '--search', *queries[offset:offset + 120]],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                               env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            if r.returncode not in (0, 1):
                raise ClosureError('source_package_mapping_failed')
            for line in r.stdout.decode('utf-8', 'replace').splitlines():
                if ': ' not in line:
                    continue
                package, path = line.rsplit(': ', 1)
                if re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?(?:, [a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?)*', package) is None:
                    continue  # dpkg diversion explanatory lines are not package owners.
                if path in aliases:
                    self.packages.setdefault(aliases[path], set()).update(package.split(', '))
        selected = sorted({p for packages in self.packages.values() for p in packages})
        for package in selected:
            name = package.split(':', 1)[0]
            copyright_path = '/usr/share/doc/' + name + '/copyright'
            if not self.maybe(copyright_path):
                raise ClosureError('selected_package_copyright_missing')
        self.maybe('/usr/share/common-licenses', tree=True)
        self.close_elfs()
        return selected

    def signature(self, path):
        info, target = self.nodes[path]
        kind = stat.S_IFMT(info.st_mode)
        metadata = (kind, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        if stat.S_ISREG(info.st_mode):
            if path not in self._fingerprints:
                digest = hashlib.sha256()
                with self.source(path).open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                self._fingerprints[path] = digest.hexdigest()
            return (*metadata, info.st_size, self._fingerprints[path])
        return (*metadata, target if stat.S_ISLNK(info.st_mode) else None)

    def delta_nodes(self, base):
        if self.root != base.root:
            # Different roots are supported for synthetic comparison tests only;
            # signatures never resolve either tree against the host filesystem.
            pass
        selected = {path: node for path, node in self.nodes.items()
                    if path not in base.nodes or self.signature(path) != base.signature(path)}
        # A new delta is a real standalone directory tree, not a whiteout layer.
        # Re-add only canonical parent directories; never copy inherited aliases.
        for path in list(selected):
            parent = str(PurePosixPath(path).parent)
            while parent != path:
                node = self.nodes.get(parent)
                if node is None or not stat.S_ISDIR(node[0].st_mode):
                    raise ClosureError('delta_parent_not_canonical_directory')
                selected[parent] = node
                if parent == '/':
                    break
                path, parent = parent, str(PurePosixPath(parent).parent)
        return selected

    def delta_summary(self, base):
        nodes = self.delta_nodes(base)
        return {'base_profile': base.profile, 'profile': self.profile,
                'delta_files': sum(stat.S_ISREG(s.st_mode) for s, _ in nodes.values()),
                'delta_bytes': sum(s.st_size for s, _ in nodes.values() if stat.S_ISREG(s.st_mode)),
                'shared_nodes': sum(path in base.nodes and self.signature(path) == base.signature(path)
                                    for path in self.nodes)}

    def write_inventory(self, output, base=None):
        root = Path(output) / 'usr/share/k3s-restic'
        root.mkdir(parents=True, exist_ok=True)
        entries = []
        for path, (info, link) in sorted(self.nodes.items()):
            kind = 'file' if stat.S_ISREG(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode) else 'symlink'
            entry = {'path': path, 'type': kind, 'bytes': info.st_size if kind == 'file' else 0,
                     'mode': oct(stat.S_IMODE(info.st_mode)), 'uid': info.st_uid, 'gid': info.st_gid}
            if kind == 'file':
                entry['sha256'] = self.signature(path)[-1]
            if link is not None:
                entry['link_target'] = link
            if path in self.packages:
                entry['source_packages'] = sorted(self.packages[path])
            entries.append(entry)
        # Metadata outputs are deliberately not recursively included in their own hash.
        (root / 'runtime-files.json').write_text(json.dumps({'schema_version': 1,
            'package_mapping': 'build-stage package owners; selected files only, not complete installed packages',
            'profile': self.profile, 'base_profile': base.profile if base else None,
            'files': entries}, separators=(',', ':')) + '\n')
        (root / 'slim-runtime.json').write_text(json.dumps({'schema_version': 1,
            'excluded_features': ['postgres_jit', 'galera'], 'runtime_file_count': len(entries),
            'elf_files': len(self.elfs), 'bytes': self.summary()['bytes'],
            'package_inventory_scope': 'selected files; packages partially copied',
            'profile': self.profile, 'base_profile': base.profile if base else None,
            'delta': self.delta_summary(base) if base else None,
            'metadata_hash_exclusions': ['runtime-files.json', 'slim-runtime.json', 'builder-packages.txt', 'profile.json']},
            sort_keys=True) + '\n')
        (root / 'profile.json').write_text(json.dumps(profile_capabilities(self.profile), sort_keys=True) + '\n')
        if self.builder_inventory:
            (root / 'builder-packages.txt').write_text('# BUILD STAGE ONLY; NOT INSTALLED RUNTIME PACKAGE CLAIM\n'
                                                     + self.builder_inventory.read_text())

    def summary(self):
        counts = {'files': 0, 'directories': 0, 'symlinks': 0, 'bytes': 0, 'elf_files': len(self.elfs)}
        for info, _ in self.nodes.values():
            if stat.S_ISREG(info.st_mode):
                counts['files'] += 1
                counts['bytes'] += info.st_size
            elif stat.S_ISDIR(info.st_mode):
                counts['directories'] += 1
            else:
                counts['symlinks'] += 1
        counts['runtime_empty_directories'] = len(self.empty_dirs)
        return counts

    @staticmethod
    def owner(path, uid, gid, *, follow=True):
        if os.geteuid() == 0:
            os.chown(path, uid, gid, follow_symlinks=follow)
        else:
            info = path.stat() if follow else path.lstat()
            if (info.st_uid, info.st_gid) != (uid, gid):
                raise ClosureError('owner_preservation_requires_root')

    def materialize(self, output, base=None):
        selected_nodes = self.nodes if base is None else self.delta_nodes(base)
        output = Path(output)
        if not output.is_absolute() or output == Path('/') or '..' in output.parts:
            raise ClosureError('invalid_output')
        if any(p.is_symlink() for p in [output, *output.parents]):
            raise ClosureError('symlink_output_forbidden')
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ClosureError('output_not_empty')
        if output.resolve() == self.root or self.root != Path('/') and self.root in output.resolve().parents:
            raise ClosureError('output_inside_source_forbidden')
        output.mkdir(mode=0o755, parents=True, exist_ok=True)
        def dest(path):
            return output.joinpath(*PurePosixPath(path).parts[1:])
        # Canonical directories FIRST: no mkdir through /bin, /lib or /lib64.
        dirs = sorted((p for p, (s, _) in selected_nodes.items() if stat.S_ISDIR(s.st_mode)),
                      key=lambda p: (len(PurePosixPath(p).parts), p))
        for path in dirs:
            dest(path).mkdir(mode=0o755, exist_ok=True)
        for path, (info, target) in sorted(selected_nodes.items()):
            if stat.S_ISLNK(info.st_mode):
                os.symlink(target, dest(path))
                self.owner(dest(path), info.st_uid, info.st_gid, follow=False)
        for path, (info, _) in sorted(selected_nodes.items()):
            if stat.S_ISREG(info.st_mode):
                shutil.copyfile(self.source(path), dest(path), follow_symlinks=False)
                shutil.copystat(self.source(path), dest(path), follow_symlinks=False)
                self.owner(dest(path), info.st_uid, info.st_gid)
                os.chmod(dest(path), stat.S_IMODE(info.st_mode))
        for path in reversed(dirs):
            info = self.nodes[path][0]
            self.owner(dest(path), info.st_uid, info.st_gid)
            os.chmod(dest(path), stat.S_IMODE(info.st_mode))
        for path, (mode, uid, gid) in self.empty_dirs.items():
            if base is not None and base.empty_dirs.get(path) == (mode, uid, gid):
                continue
            target = dest(path)
            if target.is_symlink():
                raise ClosureError('runtime_directory_symlink')
            target.mkdir(mode=mode, parents=True, exist_ok=True)
            self.owner(target, uid, gid)
            os.chmod(target, mode)
        self.write_inventory(output, base)
        return {**self.summary(), **(self.delta_summary(base) if base else {})}


PG_TOOLS = ('postgres', 'initdb', 'pg_ctl', 'psql', 'pg_dump', 'pg_dumpall', 'pg_restore',
            'pg_controldata', 'pg_basebackup', 'pg_verifybackup', 'pg_isready', 'pg_config',
            'pg_resetwal', 'pg_checksums', 'pg_waldump', 'pg_receivewal', 'pg_recvlogical',
            'createdb', 'createuser', 'dropdb', 'dropuser', 'vacuumdb', 'reindexdb', 'clusterdb')
TOOLS = ('/bin/sh', '/bin/bash', '/usr/bin/python3', '/usr/local/bin/restic',
         '/usr/bin/rsync', '/usr/bin/tar', '/usr/bin/gzip', '/usr/bin/bzip2', '/usr/bin/locale',
         '/usr/bin/flock', '/usr/bin/timeout', '/usr/bin/setpriv', '/usr/sbin/runuser',
         '/usr/sbin/cron', '/usr/bin/crontab', '/usr/bin/mariadb', '/usr/sbin/mariadbd',
         '/usr/bin/mariadb-dump', '/usr/bin/mariadb-admin', '/usr/bin/mariadb-install-db',
         '/usr/bin/mariadb-backup', '/usr/bin/my_print_defaults', '/usr/bin/resolveip',
         '/usr/bin/mariadb-upgrade', '/usr/bin/mariadb-check', '/usr/bin/hostname',
         '/usr/bin/id', '/usr/bin/logname', '/usr/bin/env', '/usr/bin/dirname', '/usr/bin/basename',
         '/usr/bin/readlink', '/usr/bin/realpath', '/usr/bin/sed', '/usr/bin/awk', '/usr/bin/grep',
         '/usr/bin/head', '/usr/bin/tail', '/usr/bin/cat', '/usr/bin/tr', '/usr/bin/cut',
         '/usr/bin/expr', '/usr/bin/test', '/usr/bin/printf', '/usr/bin/find', '/usr/bin/xargs',
         '/usr/bin/chmod', '/usr/bin/chown', '/usr/bin/mkdir', '/usr/bin/rmdir', '/usr/bin/rm',
         '/usr/bin/cp', '/usr/bin/mv', '/usr/bin/ln', '/usr/bin/touch', '/usr/bin/mktemp',
         '/usr/bin/kill', '/usr/bin/sleep', '/usr/bin/date', '/usr/bin/uname', '/usr/bin/getent',
         '/usr/bin/sort', '/usr/bin/wc', '/usr/bin/du', '/usr/bin/df', '/usr/bin/sync',
         '/usr/bin/true', '/usr/bin/false', '/usr/bin/sha256sum', '/usr/bin/curl',
         '/usr/local/bin/image-selftest')


PROFILES = ('files', 'lite', 'all')
MARIA_TOOLS = tuple(path for path in TOOLS if Path(path).name.startswith(('mariadb', 'my_print_defaults', 'resolveip')))
COMMON_TOOLS = tuple(path for path in TOOLS if path not in MARIA_TOOLS)


def profile_capabilities(profile):
    if profile not in PROFILES:
        raise ClosureError('unknown_profile')
    majors = ['16', '17'] if profile == 'all' else []
    return {'schema_version': 1, 'profile': profile, 'postgres_majors': majors,
            'mariadb_series': ['11.8'] if profile == 'all' else [], 'files': True}


def runtime_plan(c, profile='all'):
    c.profile = profile
    capabilities = profile_capabilities(profile)
    for path in ('/bin', '/sbin', '/lib'):
        c.add(path)
    c.maybe('/lib64')
    for path in COMMON_TOOLS + (MARIA_TOOLS if capabilities['mariadb_series'] else ()):
        c.add(path)
    for major in capabilities['postgres_majors']:
        for binary in PG_TOOLS:
            c.add('/usr/lib/postgresql/' + major + '/bin/' + binary)
        c.pattern('/usr/lib/postgresql/' + major + '/lib/*.so', required=True)
        c.add('/usr/share/postgresql/' + major, tree=True)
        c.add('/usr/lib/postgresql/' + major + '/lib/vector.so')
        c.add('/usr/share/postgresql/' + major + '/extension/vector--0.8.2.sql')
    if capabilities['mariadb_series']:
        c.add('/usr/share/mariadb', tree=True)
        c.add('/usr/lib/mysql/plugin', tree=True)
    for path in ('/usr/lib/python3.11',
                 '/usr/lib/locale', '/usr/share/zoneinfo', '/usr/lib/terminfo',
                 '/etc/ssl/certs', '/usr/share/ca-certificates', '/etc/pam.d',
                 '/opt/k3s-restic/k3s_backup', '/usr/share/k3s-restic'):
        c.add(path, tree=True)
    # The one synthetic harness is an explicit CI/runtime verification entrypoint.
    c.add('/opt/k3s-restic/tests/container_e2e.py')
    for path in ('/etc/passwd', '/etc/group', '/etc/nsswitch.conf', '/etc/login.defs',
                 '/etc/ssl/openssl.cnf', '/etc/localtime', '/etc/timezone'):
        c.add(path)
    for path in ('/usr/lib/ssl/cert.pem', '/usr/lib/ssl/openssl.cnf', '/etc/ld.so.cache',
                 '/etc/ld.so.conf', '/etc/environment', '/etc/locale.alias',
                 '/usr/share/locale/locale.alias'):
        c.maybe(path)
    for pattern in ('/usr/lib/*-linux-gnu/gconv', '/usr/lib/*-linux-gnu/security',
                    '/usr/lib/*-linux-gnu/ossl-modules', '/usr/lib/*-linux-gnu/libnss_*.so*',
                    '/usr/lib/*-linux-gnu/sasl2', '/usr/lib/*-linux-gnu/krb5/plugins',
                    '/etc/security/*.conf', '/etc/security/limits.d', '/etc/ld.so.conf.d',
                    '/usr/sbin/unix_chkpwd',
                    '/usr/sbin/unix_update', '/etc/krb5.conf'):
        c.pattern(pattern, tree=True)
    if capabilities['postgres_majors']:
        c.pattern('/usr/share/postgresql-common', tree=True)
    c.pattern('/usr/lib/*-linux-gnu/gconv', required=True, tree=True)
    c.pattern('/usr/lib/*-linux-gnu/libnss_files.so*', required=True)
    c.pattern('/usr/lib/python3.11/lib-dynload/_ssl*.so', required=True)
    c.pattern('/usr/lib/python3.11/lib-dynload/_ctypes*.so', required=True)
    c.builder_inventory = c.source('/usr/share/k3s-restic/packages.txt')
    c.nodes.pop('/usr/share/k3s-restic/packages.txt', None)
    if not capabilities['postgres_majors']:
        c.nodes.pop('/usr/share/k3s-restic/pgdg-downloads.lock', None)
    if not capabilities['mariadb_series']:
        c.nodes.pop('/usr/share/k3s-restic/mariadb-downloads.lock', None)
    c.close_elfs()
    c.add_package_licenses()
    for path, mode, uid, gid in (('/tmp', 0o1777, 0, 0), ('/run', 0o755, 0, 0),
                                 ('/var/tmp', 0o1777, 0, 0), ('/var/log', 0o755, 0, 0),
                                 ('/data', 0o700, 0, 0), ('/backup', 0o700, 0, 0),

                                 ('/var/spool/cron/crontabs', 0o1730, 0, 0),
                                 ('/etc/ssl/private', 0o700, 0, 0)):
        c.empty_dirs[path] = (mode, uid, gid)
    if capabilities['postgres_majors']:
        c.empty_dirs['/var/lib/postgresql'] = (0o700, 999, 999)
    if capabilities['mariadb_series']:
        c.empty_dirs['/var/lib/mysql'] = (0o700, 998, 998)
    validate_profile_paths(c)
    return c


def validate_profile_paths(c):
    capabilities = profile_capabilities(c.profile)
    for path in c.nodes:
        if path.startswith('/usr/share/doc/'):
            continue  # source-package attribution is not shipped database runtime
        major = re.search(r'/(?:postgresql|postgresql-)(?:/)?(16|17)(?:/|$)', path)
        if major and major.group(1) not in capabilities['postgres_majors']:
            raise ClosureError('foreign_postgres_resource_in_profile')
        if not capabilities['postgres_majors'] and any(x in path for x in
                ('/postgresql', '/libpq.so', '/libpqwalreceiver', '/pg_config')):
            raise ClosureError('postgres_resource_in_non_postgres_profile')
        if not capabilities['mariadb_series'] and any(x in path for x in
                ('/mariadb', '/mysql/', '/my_print_defaults', '/resolveip', '/libmariadb')):
            raise ClosureError('mariadb_resource_in_non_mariadb_profile')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', nargs='?')
    parser.add_argument('--output')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--profile', choices=PROFILES, default='all')
    parser.add_argument('--base-profile', choices=('files',))
    args = parser.parse_args(argv)
    if args.destination and args.output:
        raise ClosureError('ambiguous_output')
    output = args.output or args.destination
    if not args.dry_run and not output:
        raise ClosureError('output_required')
    if args.base_profile and args.profile in {'files', 'lite'}:
        raise ClosureError('profile_equals_base')
    c = runtime_plan(Collector(), args.profile)
    base = runtime_plan(Collector(), args.base_profile) if args.base_profile else None
    result = ({**c.summary(), **(c.delta_summary(base) if base else {})}
              if args.dry_run else c.materialize(output, base))
    print(json.dumps({'status': 'planned' if args.dry_run else 'collected', 'profile': c.profile, **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ClosureError, OSError) as error:
        code = str(error) if isinstance(error, ClosureError) else 'filesystem_operation_failed'
        print(json.dumps({'status': 'failed', 'code': code}), file=__import__('sys').stderr)
        raise SystemExit(1)
