import sys, paramiko

KEY = r"C:\Users\ASUS\Downloads\yuda001.pem"
HOST = "1.15.79.119"
USER = "ubuntu"

def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "whoami"
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    if arg.startswith("@"):
        cmd = open(arg[1:], "r", encoding="utf-8").read()
    else:
        cmd = arg
    pkey = paramiko.RSAKey.from_private_key_file(KEY)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, pkey=pkey, timeout=20, banner_timeout=20, auth_timeout=20,
                look_for_keys=False, allow_agent=False)
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout, get_pty=True)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    if err.strip():
        sys.stdout.write("\n[stderr]\n" + err)
    print(f"\n[exit {code}]")
    cli.close()

main()
