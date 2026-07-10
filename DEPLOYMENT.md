# 部署说明

本文记录 `trip` 在阿里云 ECS 上的 systemd、Nginx 和 HTTPS 证书配置方法。

## systemd 常驻运行

生产环境建议把项目放在 `/opt/trip`，通过 systemd 常驻运行。服务端 `.env` 示例：

```ini
HOST=0.0.0.0
PORT=8081
```

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
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

启用配置：

```bash
ln -sf /etc/nginx/sites-available/trip /etc/nginx/sites-enabled/trip
nginx -t
systemctl reload nginx
```

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
