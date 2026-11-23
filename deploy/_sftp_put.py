import sys, paramiko, os, time

KEY = r"C:\Users\ASUS\Downloads\yuda001.pem"
HOST = "1.15.79.119"
USER = "ubuntu"

def main():
    local = sys.argv[1]
    remote = sys.argv[2]
    pkey = paramiko.RSAKey.from_private_key_file(KEY)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, pkey=pkey, timeout=20, banner_timeout=30, auth_timeout=20,
                look_for_keys=False, allow_agent=False)
    sftp = cli.open_sftp()
    size = os.path.getsize(local)
    t0 = time.time()
    sftp.put(local, remote, callback=lambda done, total: None)
    st = sftp.stat(remote)
    print(f"uploaded {size} bytes -> {remote} ({st.st_size} on server) in {time.time()-t0:.1f}s")
    sftp.close()
    cli.close()

main()
