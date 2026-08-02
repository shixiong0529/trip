# 部署说明

本文记录 `trip` 在阿里云 ECS 上的 systemd、Nginx 和 HTTPS 证书配置方法。

## systemd 常驻运行

生产环境建议把项目放在 `/opt/trip`，通过 systemd 常驻运行。服务端 `.env` 示例：

```ini
HOST=0.0.0.0
PORT=8081

# 专业版发布前自动审核、诊断并修复主要问题
PRO_REVIEW_MODE=repair
PRO_REVIEW_TIMEOUT=60
PRO_REVIEW_MAX_TOKENS=2500
PRO_REVIEW_TOTAL_TIMEOUT=420
PRO_REWRITE_MAX_ATTEMPTS=1
```

`PRO_REVIEW_MODE` 支持 `off`、`shadow`、`audit`、`repair`。生产默认使用 `repair`；如需紧急回退，可改为 `off` 后重启服务。`shadow` 会完成诊断并记录指标，但仍发布初稿；`audit` 对需要重写或重规划的主要/严重问题拦截发布，不执行模型重写。实时开放、票价等只能人工核实的问题不会交给模型猜测。

创建服务：

```bash
cat > /etc/systemd/system/trip.service <<'EOF'
[Unit]
Description=Trip AI Travel Planner
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/trip
EnvironmentFile=/opt/trip/.env
ExecStart=/opt/trip/.venv/bin/python /opt/trip/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now trip
curl http://127.0.0.1:8081/api/health
```

## Nginx 反向代理

`trip.moyu.in` 的示例配置：

```nginx
server {
    listen 80;
    server_name trip.moyu.in;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name trip.moyu.in;

    ssl_certificate     /etc/letsencrypt/live/trip.moyu.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/trip.moyu.in/privkey.pem;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
        # 专业版包含初稿、审核和最多一次修复，需覆盖 420 秒业务总时限。
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

启用配置：

```bash
ln -sf /etc/nginx/sites-available/trip /etc/nginx/sites-enabled/trip
nginx -t
systemctl reload nginx
```

专业版审核期间仍通过 SSE 返回进度。反向代理必须关闭缓冲，并将读写超时保持在 `PRO_REVIEW_TOTAL_TIMEOUT` 之上；默认建议 600 秒。修改 `.env` 后执行：

```bash
systemctl restart trip
journalctl -u trip -n 100 --no-pager
```

日志应能看到专业版的审核结论及是否触发修复。标准版不会进入该流程。若新模型的诊断 JSON 不稳定，可临时切换为 `PRO_REVIEW_MODE=shadow` 收集结果，或切换为 `off` 完全关闭审核。

## HTTPS 证书

这台阿里云 ECS 上 Certbot 的 HTTP-01 验证可能会被云安全中心/Aegis 拦截成 `403`，即使本机 Nginx 配置正确也会失败。因此证书统一使用 `acme.sh + DNS-01`，由阿里云 DNS API 自动创建 TXT 记录，后续由 cron 自动续期。

首次安装 `acme.sh`：

```bash
curl https://get.acme.sh | sh -s email=你的邮箱
```

使用 `moyu.in` 所属阿里云账号的 RAM AccessKey。该 RAM 子账号建议只授予 DNS 管理权限，不要使用主账号 AccessKey。

```bash
export Ali_Key="你的-AccessKey-ID"
export Ali_Secret="你的-AccessKey-Secret"

~/.acme.sh/acme.sh --issue --dns dns_ali -d trip.moyu.in \
  --server letsencrypt --dnssleep 30
```

签发成功后安装到 Nginx 引用路径：

```bash
mkdir -p /etc/letsencrypt/live/trip.moyu.in

~/.acme.sh/acme.sh --install-cert -d trip.moyu.in \
  --key-file       /etc/letsencrypt/live/trip.moyu.in/privkey.pem \
  --fullchain-file /etc/letsencrypt/live/trip.moyu.in/fullchain.pem \
  --reloadcmd      "nginx -t && systemctl reload nginx"
```

## 多账号续期约定

`dns_ali` 会把 `Ali_Key` / `Ali_Secret` 保存到 acme.sh 配置目录。若同一台服务器管理多个阿里云账号的域名，应为不同账号使用不同 `--config-home`，避免后一次签发覆盖前一次的 DNS AccessKey。

当前约定：

| 域名 | 账号 | 配置目录 | cron |
|---|---|---|---|
| `moyu.in` / `www.moyu.in` | 国际站 | 默认 `~/.acme.sh` | 每天 18:12 |
| `gaokao.moyu.in` | 国际站 | 默认 `~/.acme.sh` | 每天 18:12 |
| `trip.moyu.in` | 国际站 | 默认 `~/.acme.sh` | 每天 18:12 |
| `chat.slow.best` | 国际站 | 默认 `~/.acme.sh` | 每天 18:12 |
| `shi.show` / `www.shi.show` | 中国站 | `/root/.acme.sh-china` | 每天 18:13 |

检查证书和自动续期：

```bash
~/.acme.sh/acme.sh --list
~/.acme.sh/acme.sh --list --config-home /root/.acme.sh-china
crontab -l | grep acme
curl https://trip.moyu.in/api/health
```

## 当前线上检查结果

最近一次检查结果：

| 域名 | HTTPS | 证书到期 |
|---|---:|---:|
| `gaokao.moyu.in` | 正常，200 | 2026-09-30 |
| `moyu.in` | 正常，200 | 2026-09-30 |
| `www.moyu.in` | 正常，200 | 2026-09-30 |
| `chat.slow.best` | 正常，200 | 2026-09-30 |
| `shi.show` | 正常，200 | 2026-09-30 |
| `www.shi.show` | 正常，200 | 2026-09-30 |
| `trip.moyu.in` | 正常，200 | 2026-10-08 |
