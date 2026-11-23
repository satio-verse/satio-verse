"""Build a pinned release or apply/rollback it while WorldModel is stopped."""
import argparse
import base64
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wm.platform_services import ReleaseUpdater, canonical, digest


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--root', type=Path, required=True)
    build.add_argument('--version', required=True)
    build.add_argument('--output', type=Path, required=True)
    build.add_argument('files', nargs='+')
    for command in ('apply', 'rollback'):
        run = sub.add_parser(command)
        run.add_argument('--root', type=Path, required=True)
        run.add_argument('--home', type=Path, required=True)
        run.add_argument('--processes-stopped', action='store_true', required=True,
                         help='Only after stopping backend and native host; this tool does not stop them')
    args = parser.parse_args()
    if args.command == 'build':
        files = []
        for name in args.files:
            path = (args.root / name).resolve()
            if not path.is_relative_to(args.root.resolve()):
                raise ValueError('文件越出安装目录')
            raw = path.read_bytes()
            files.append({'path': path.relative_to(args.root.resolve()).as_posix(), 'sha256': digest(raw),
                          'content': base64.b64encode(raw).decode()})
        value = {'worldmodel_release': 1, 'version': args.version, 'files': files}
        args.output.write_bytes(canonical(value))
        print(json.dumps({'file': str(args.output), 'trusted_manifest_sha256': digest(canonical(value))}))
    else:
        updater = ReleaseUpdater(args.home, args.root)
        print(json.dumps(getattr(updater, args.command)(offline=True), ensure_ascii=False))


if __name__ == '__main__':
    main()
