import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
import pytest
from tools.package_platform import FILES, build
from tools.platform_admin import provision


def test_deploy_zip_has_only_sources_and_cli_help_works(tmp_path):
    package=tmp_path/'platform.zip'
    report=build(package)
    assert len(report['sha256'])==64
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist())==set(FILES)|{'README.md','CONTENTS.sha256'}
        checks=archive.read('CONTENTS.sha256').decode().splitlines()
        for line in checks:
            wanted,name=line.split('  ',1)
            assert hashlib.sha256(archive.read(name)).hexdigest()==wanted
        assert not any(name.endswith(('.pyc','.exe','.dll')) or '/private/' in name for name in archive.namelist())
        destination=tmp_path/'extracted';archive.extractall(destination)
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    for tool in ('community_server','release_bundle','platform_admin','sign_release'):
        result=subprocess.run([sys.executable,str(destination/'tools'/f'{tool}.py'),'--help'],cwd=destination,
                              env=env,capture_output=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert result.returncode==0,result.stderr
        assert b'usage:' in result.stdout


def test_provisioning_is_private_and_never_overwrites(tmp_path):
    private=tmp_path/'private'
    first=provision(private,'alice')
    original=(private/'receipt-secret.txt').read_text()
    token=(private/'alice-token.txt').read_text()
    assert token not in json.dumps(first) and original not in json.dumps(first)
    authors=json.loads((private/'authors.json').read_text())
    assert authors[hashlib.sha256(token.encode()).hexdigest()]=='alice'
    with pytest.raises(ValueError):provision(private,'alice')
    provision(private,'bob')
    assert (private/'receipt-secret.txt').read_text()==original
    assert (private/'alice-token.txt').read_text()==token
    with pytest.raises(ValueError):provision(private,'../escape')
