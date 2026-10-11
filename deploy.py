"""拾光部署脚本:Windows -> zx0094 (Ubuntu 24.04)
用法:
  python deploy.py setup    # 首次:建目录、venv、env 文件、systemd、防火墙
  python deploy.py update   # 更新代码并重启服务
运行前设置环境变量:
  SHIGUANG_PASSWORD  登录密码
  ZHIPU_API_KEY      智谱 API Key
  DEPLOY_PASS        服务器 SSH 密码(或已配好密钥则不需要)
"""
import os
import sys

import paramiko

HOST = "221.239.50.139"
PORT = 38022
USER = "zx0094"
REMOTE = "/opt/shiguang"

UPLOAD = [
    "app/__init__.py", "app/config.py", "app/db.py", "app/auth.py",
    "app/llm.py", "app/parser.py", "app/export.py", "app/main.py",
    "static/index.html", "static/style.css", "static/app.js", "static/manifest.webmanifest",
    "requirements.txt",
]

SERVICE = """[Unit]
Description=Shiguang personal log agent
After=network-online.target

[Service]
User={user}
WorkingDirectory={dir}
EnvironmentFile={dir}/shiguang.env
ExecStart={dir}/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8300
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
""".format(user=USER, dir=REMOTE)


def connect():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pw = os.environ.get("DEPLOY_PASS")
    cli.connect(HOST, port=PORT, username=USER, password=pw)
    return cli


def run(cli, cmd, check=True):
    print("$", cmd)
    _, out, err = cli.exec_command(cmd, timeout=600)
    o = out.read().decode()
    e = err.read().decode()
    rc = out.channel.recv_exit_status()
    if o.strip():
        print(o.rstrip())
    if e.strip():
        print("[stderr]", e.rstrip())
    if check and rc != 0:
        raise SystemExit("命令失败 (rc=%d): %s" % (rc, cmd))
    return o


def upload(cli):
    sftp = cli.open_sftp()
    base = os.path.dirname(os.path.abspath(__file__))
    for rel in UPLOAD:
        local = os.path.join(base, rel.replace("/", os.sep))
        remote = REMOTE + "/" + rel
        run(cli, "mkdir -p " + os.path.dirname(remote), check=False)
        print("put", rel)
        sftp.put(local, remote)
    sftp.close()


def setup(cli):
    run(cli, "sudo mkdir -p %s && sudo chown %s:%s %s" % (REMOTE, USER, USER, REMOTE))
    upload(cli)
    run(cli, "python3 -m venv %s/venv" % REMOTE)
    run(cli, "%s/venv/bin/pip install -q -r %s/requirements.txt" % (REMOTE, REMOTE))
    env = (
        "SHIGUANG_PASSWORD=%s\nZHIPU_API_KEY=%s\nGLM_MODEL=%s\nPORT=8300\n" % (
            os.environ.get("SHIGUANG_PASSWORD", ""),
            os.environ.get("ZHIPU_API_KEY", ""),
            os.environ.get("GLM_MODEL", "glm-4-flash"),
        )
    )
    if not os.environ.get("SHIGUANG_PASSWORD") or not os.environ.get("ZHIPU_API_KEY"):
        raise SystemExit("请先设置 SHIGUANG_PASSWORD 和 ZHIPU_API_KEY 环境变量")
    sftp = cli.open_sftp()
    with sftp.open(REMOTE + "/shiguang.env", "w") as f:
        f.write(env)
    sftp.close()
    run(cli, "chmod 600 %s/shiguang.env" % REMOTE)
    sftp = cli.open_sftp()
    with sftp.open("/tmp/shiguang.service", "w") as f:
        f.write(SERVICE)
    sftp.close()
    run(cli, "sudo mv /tmp/shiguang.service /etc/systemd/system/shiguang.service")
    run(cli, "sudo systemctl daemon-reload && sudo systemctl enable --now shiguang")
    run(cli, "sudo ufw allow 8300/tcp || true")
    run(cli, "sleep 2 && systemctl --no-pager status shiguang | head -12")


def update(cli):
    upload(cli)
    run(cli, "%s/venv/bin/pip install -q -r %s/requirements.txt" % (REMOTE, REMOTE))
    run(cli, "sudo systemctl restart shiguang && sleep 2 && systemctl is-active shiguang")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "update"
    cli = connect()
    try:
        if action == "setup":
            setup(cli)
        else:
            update(cli)
    finally:
        cli.close()
    print("done:", action)
