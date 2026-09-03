"""一键把阿里云免费证书装到服务器并启用 HTTPS。

用法：
    python scripts/install_cert.py <证书zip路径>
    python scripts/install_cert.py --auto          # 自动在「下载」目录找最新的证书 zip

流程：解压 zip → 识别证书链(.pem)与私钥(.key) → 上传到服务器 deploy/certs/
      → 启动 Nginx 容器 → 公网验证 https://api.tiaowulan.com/health
"""
import glob
import os
import subprocess
import sys
import zipfile

SERVER = 'admin@8.138.203.6'
REMOTE_BASE = '/opt/flora_agent_package'
DOMAIN = 'api.tiaowulan.com'
DOWNLOADS = os.path.join(os.path.expanduser('~'), 'Downloads')


def run(cmd, **kw):
    print(f'$ {" ".join(cmd)}')
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)


def find_zip(arg):
    if arg and arg != '--auto':
        return arg
    cands = []
    for p in glob.glob(os.path.join(DOWNLOADS, '*.zip')):
        name = os.path.basename(p)
        if DOMAIN in name or '证书' in name or name.startswith('cert'):
            cands.append(p)
    if not cands:
        cands = [p for p in glob.glob(os.path.join(DOWNLOADS, '*.zip'))]
    if not cands:
        sys.exit(f'[FAIL] 在 {DOWNLOADS} 没找到证书 zip，请手动指定路径')
    return max(cands, key=os.path.getmtime)


def extract(zip_path):
    tmp = os.path.join(os.path.dirname(zip_path), '_cert_extract')
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    pem = key = None
    for root, _dirs, files in os.walk(tmp):
        for f in files:
            low = f.lower()
            full = os.path.join(root, f)
            if low.endswith('.key'):
                key = full
            elif low.endswith('.pem'):
                pem = full
    if not (pem and key):
        sys.exit(f'[FAIL] zip 里没找到 .pem / .key：{os.listdir(tmp)}')
    print(f'  证书链: {os.path.basename(pem)}\n  私钥  : {os.path.basename(key)}')
    return pem, key


def main():
    zip_path = find_zip(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f'[1/5] 使用证书包: {zip_path}')
    pem, key = extract(zip_path)

    print('[2/5] 上传到服务器 deploy/certs/')
    for local, remote in ((pem, 'fullchain.pem'), (key, 'privkey.pem')):
        r = run(['scp', '-o', 'BatchMode=yes', local, f'{SERVER}:{REMOTE_BASE}/deploy/certs/{remote}'])
        if r.returncode != 0:
            sys.exit(f'[FAIL] 上传失败: {r.stderr}')

    print('[3/5] 校验证书（域名匹配 + 到期时间）')
    r = run(['ssh', '-o', 'BatchMode=yes', SERVER,
             f'cd {REMOTE_BASE} && chmod 600 deploy/certs/* && '
             f'openssl x509 -in deploy/certs/fullchain.pem -noout -subject -enddate && '
             f'openssl x509 -in deploy/certs/fullchain.pem -noout -checkend 0'])
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        sys.exit('[FAIL] 证书无效或已过期')

    print('[4/5] 启动 Nginx（80/443）')
    r = run(['ssh', '-o', 'BatchMode=yes', SERVER,
             f'cd {REMOTE_BASE} && sudo docker compose --profile nginx up -d 2>&1 | tail -5'])
    print(r.stdout.strip() or r.stderr.strip())

    print('[5/5] 公网验证')
    r = run(['curl', '-s', '-m', '10', f'https://{DOMAIN}/health'])
    body = r.stdout.strip()
    if '"status":"ok"' in body:
        print(f'HTTPS OK -> {body}')
        print('\n[完成] 小程序正式接入地址: https://' + DOMAIN)
    else:
        print(f'[WARN] 通过域名访问未成功（{body or r.stderr.strip()}），检查：DNS 是否生效 / Nginx 是否启动 / 443 是否放行')


if __name__ == '__main__':
    main()
