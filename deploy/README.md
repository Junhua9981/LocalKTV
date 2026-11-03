# Oracle VPS 部署（Ubuntu 24.04、公網 IPv4、無網域）

架構：手機／現場 PC → HTTPS Nginx :443 `/ktv/` → LocalKTV 127.0.0.1:8080。
VPS 負責搜尋、下載、快取與排隊；現場 PC 開 `/display` 才會播放聲音。
請只跑 **一個** LocalKTV process/worker，Queue 與登入 session 都由它持有。

登入方式（三種身分，權限由伺服端的登入 session 決定，不看頁面自己宣告的角色）：

| 身分 | 怎麼進來 | 可以用 | 有效期 |
|---|---|---|---|
| 管理員 | 首頁或 `/login` 輸入管理員密碼（第一次在 `/setup` 設定） | 房間列表 `/`、每間房的 `/r/<房號>/host`、`remote`、`display`、`/admin` | 30 天（`KTV_ADMIN_DAYS`） |
| 電視（借來的筆電） | 沒登入的裝置開首頁會顯示配對 QR，管理員手機掃了按確定（或在 `/admin` 輸入 6 位數配對碼） | 那間房的 `/r/<房號>/display` | `KTV_PARTY_HOURS`（12 小時） |
| 客人 | 掃 Display 上的點歌 QR | 那間房的 `/r/<房號>/remote`，只能刪自己點的歌 | 到那間這場結束，最長 12 小時（`KTV_PARTY_HOURS`） |

多房間（見根目錄 README「多房間」）：每間房各自開場、各有自己的點歌 QR；「結束這場」只登出那間的客人；刪除房間會登出那間的客人與電視。

在家裡用的流程：筆電當螢幕、手機當遙控器。管理員自己的筆電直接用密碼登入，首頁按「這台當螢幕」；
朋友的筆電則在首頁掃配對 QR。螢幕打開就自動開場、顯示點歌 QR；QR 每 5 分鐘更換（`KTV_INVITE_MINUTES`），
截圖傳出去的 QR 幾分鐘後就失效，已加入的客人不受影響。
那間最後一台螢幕關掉 `KTV_PARTY_IDLE_MINUTES`（預設 30）分鐘後這場自動結束、客人登出；`/admin` 也可以手動「結束這場」。
`/admin` 也列出所有登入中的裝置，可以單獨登出；登出／到期會立刻中止該裝置的 WebSocket。
登入資料存在 SQLite（只存 token 的雜湊值），重啟服務不需要重新登入。
Cookie 是 HttpOnly、Secure、SameSite=Strict；客人和電視只能透過分角色檢查的 WebSocket 操作，不能呼叫會修改資料的 HTTP API。

以下 `YOUR_PUBLIC_IP` 全部換成 Oracle VPS 的固定公網 IPv4。
範例假設這台 VPS 尚未配置其他網站；已有 Nginx 網站時請合併設定，保留原有網站。

## 1. 在 Oracle 與 Ubuntu 開放入口

Oracle Console 的 VNIC 所屬 NSG／Subnet Security List 新增 **stateful ingress**：

| TCP 目的 port | 來源 | 用途 |
|---|---|---|
| 22 | 你的管理 IP/32 | SSH，保留原有連線設定 |
| 80 | 0.0.0.0/0 | IP 憑證驗證、HTTPS 轉址 |
| 443 | 0.0.0.0/0 | HTTPS／WSS |

**不要開放 8080 或 Vite 5173。** 若已有寬鬆的全 port 規則，請收斂；有 IPv6 時也要檢查對應規則。
Ubuntu 主機內的防火牆是另一層，也需放行 80/443。不要清空 Oracle 映像既有 iptables 規則。
若使用 UFW，先允許 SSH 再啟用，避免失去管理連線：

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw status verbose
# 僅在確認 SSH 已允許、且決定使用 UFW 管理這台主機時執行：
sudo ufw enable
```

Oracle 官方：[Security Rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm)。

## 2. 從本機建置並上傳目前版本

在 Windows 的 LocalKTV 根目錄執行，先確保兩個 npm 步驟成功：

```powershell
npm.cmd --prefix web run typecheck
npm.cmd --prefix web run build
New-Item -ItemType Directory -Force temp
tar -czf temp/localktv-source.tar.gz --exclude=.git --exclude=.venv --exclude=node_modules --exclude=data --exclude=models --exclude=temp --exclude=.env --exclude='.env.*' --exclude='*.env' .
scp temp/localktv-source.tar.gz ubuntu@YOUR_PUBLIC_IP:/home/ubuntu/
```

如 SSH 私鑰不在預設位置，為 scp 加 `-i C:\路徑\你的私鑰`。
此封裝包含已建好的 `web/dist`，VPS **不需要 Node.js**。
音源外掛 `server/plugins/ktv_sources/` 若有放在本機，也會一起打包（`--exclude=.git` 會略過它自己的 git 資料夾）；沒有的話 VPS 上只會有 YouTube。
不包含歌曲快取、供應商 cookies 或本機密鑰；現有歌詞／歌曲要另行搬入 `/var/lib/localktv`，
SQLite 資料庫請在本機服務停止後連同必要資料搬移，避免複製一半的資料。

## 3. VPS 安裝服務

以下在 SSH 登入 VPS 後執行：

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg nginx snapd
sudo useradd --system --home /var/lib/localktv --shell /usr/sbin/nologin localktv
sudo install -d /opt/localktv
sudo tar -xzf /home/ubuntu/localktv-source.tar.gz -C /opt/localktv
sudo install -d -o localktv -g localktv /var/lib/localktv /opt/localktv/models
sudo python3 -m venv /opt/localktv/.venv
sudo /opt/localktv/.venv/bin/pip install -r /opt/localktv/server/requirements.txt
sudo install -m 600 /opt/localktv/deploy/localktv.env.example /etc/localktv.env
sudo nano /etc/localktv.env
```

設定檔確認以下幾項：

```ini
KTV_PUBLIC_URL=https://YOUR_PUBLIC_IP
KTV_BASE_PATH=/ktv
KTV_AUTH_REQUIRED=1
KTV_COOKIE_SECURE=1
```

管理員密碼不用在這裡設：服務第一次啟動（下面的 systemctl 之後，第 4 節 HTTPS 設好）時，
用手機開 `https://YOUR_PUBLIC_IP/ktv/`，會進到「首次設定」頁，輸入設定碼和新密碼（至少 10 個字元）就好。
設定碼印在服務紀錄裡，每次啟動都不同：

```bash
sudo journalctl -u localktv | grep 設定碼 | tail -1
```

密碼存在 `/var/lib/localktv/ktv.db`；之後在 `/admin` 最下面「更改管理員密碼」。
忘記密碼時才需要 SSH：
`sudo systemctl stop localktv && sudo -u localktv KTV_DATA_DIR=/var/lib/localktv /opt/localktv/.venv/bin/python /opt/localktv/server/main.py --set-admin-password && sudo systemctl start localktv`
（`sudo -u localktv` 讓資料庫維持服務帳號擁有；不用讀 `/etc/localktv.env`）。
網站放在 `https://YOUR_PUBLIC_IP/ktv/`（`KTV_BASE_PATH`，須與 nginx.conf 的 `/ktv` 一致；留空則放在網址最外層，Nginx 也要改回 `location /`）。
`KTV_PUBLIC_URL` 必須是瀏覽器實際使用的 origin，不能帶 `/remote` 等路徑；這也是 CSRF／WS Origin 驗證的依據。
還沒設定管理員密碼時，除了 `/setup` 什麼都打不開；使用 HTTP（且沒設 `KTV_COOKIE_SECURE=0`）時服務拒絕啟動，不會悄悄退回公開模式。
從舊版共用密鑰升級：保留 `KTV_ACCESS_KEY` 的話，第一次啟動會把它設為管理員密碼，之後請在 `/admin` 換掉並刪除該行。
`KTV_AI_ENABLED=0` 適合沒有 GPU 的 VPS；目前 AI separator 仍是 stub，Instant 模式照常可用。

```bash
sudo install -m 644 /opt/localktv/deploy/localktv.service /etc/systemd/system/localktv.service
sudo systemctl daemon-reload
sudo systemctl enable --now localktv
sudo systemctl status localktv --no-pager
```

## 4. 公網 IP 的 HTTPS 憑證

使用 **Certbot 5.4 以上** 的 webroot 模式取得 IP 憑證。
IP 憑證使用 `shortlived` profile，有效期約六天，所以續期與 Nginx reload 都必須正常。
以下從官方 Snap 安裝；不要改用 Ubuntu 24.04 舊版 apt Certbot：

```bash
sudo snap install --classic certbot
/snap/bin/certbot --version
sudo install -d -m 755 /var/www/letsencrypt
sudo cp /opt/localktv/deploy/nginx-bootstrap.conf /etc/nginx/sites-available/localktv
sudo nano /etc/nginx/sites-available/localktv
# 將 YOUR_PUBLIC_IP 換成你的 IP
sudo ln -s /etc/nginx/sites-available/localktv /etc/nginx/sites-enabled/localktv
sudo nginx -t
sudo systemctl reload nginx
```

此時 `http://YOUR_PUBLIC_IP` 回 404 是預期的，HTTP 只提供憑證驗證路徑。
若 80/443 有其他程式佔用，先解決入口衝突。用以下命令申請憑證，email 換成你的實際 email：

```bash
sudo /snap/bin/certbot certonly --webroot --webroot-path /var/www/letsencrypt \
  --preferred-profile shortlived --ip-address YOUR_PUBLIC_IP \
  --cert-name localktv --email YOUR_EMAIL --agree-tos --non-interactive
```

接著切換到正式 HTTPS 設定：

```bash
sudo cp /opt/localktv/deploy/nginx.conf /etc/nginx/sites-available/localktv
sudo nano /etc/nginx/sites-available/localktv
# 將兩個 server_name 與轉址中的 YOUR_PUBLIC_IP 都換成你的 IP
sudo nginx -t
sudo systemctl reload nginx
```

設定每次續期成功後 reload Nginx：

```bash
sudo install -d /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/localktv-nginx.sh >/dev/null <<'EOF'
#!/bin/sh
set -eu
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
EOF
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/localktv-nginx.sh
sudo /snap/bin/certbot renew --dry-run --run-deploy-hooks
systemctl list-timers --all | grep certbot
```

保留對外 80 的 ACME 路徑供续期使用。若 `dry-run` 失敗或找不到續期 timer，先修復再投入使用。
IP 改變需要重新申請憑證、修改 Nginx 與 `KTV_PUBLIC_URL`，建議使用 Oracle reserved public IP。
有網域時可用同一套設定，把站點和 PUBLIC_URL 改成網域，Certbot 申請改用 `-d 你的網域`（取代 `--ip-address`）。

官方說明：[IP 憑證與 Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)、
[Certbot Snap 安裝](https://certbot.eff.org/instructions?ws=nginx&os=snap)、
[Nginx WebSocket 設定](https://nginx.org/en/docs/http/websocket.html)。

## 5. 驗收

在自己的 PC／手機：

1. 手機開 `https://YOUR_PUBLIC_IP/ktv/`，憑證沒有警告，出現「首次設定」。錯誤設定碼不行；用 `journalctl` 裡的設定碼設好密碼後直接進首頁（「這台當螢幕／這台當遙控器」）。
2. 筆電（沒登入）開同一個網址，顯示配對 QR 和管理員登入。手機掃 QR、按確定，筆電幾秒內進入螢幕畫面並出現點歌 QR（自動開場）。
3. 另一台手機掃點歌 QR，直接進入 `/r/main/remote` 可點歌；改開 `/host` 或 `/admin` 會被導回 `/r/main/remote`。
3a. 在首頁新增一間房（例：`vip`），用另一台筆電配對到它；那台的點歌 QR 只能進 `/r/vip/remote`，兩間的清單互不影響。
4. 等 QR 更換兩次（約 10 分鐘）後，用舊 QR 的截圖掃描應顯示「這個 QR 已經失效」。
5. 用無痕視窗開 `/api/session`、`/api/qr.svg`、實際音檔 URL，應回 401。
6. 關掉筆電的螢幕分頁 30 分鐘後，客人手機的頁面中斷並回到登入頁；`/admin` 單獨登出螢幕，筆電回到配對畫面。
7. `sudo systemctl restart localktv` 後，管理員與螢幕不需要重新登入。
8. VPS 上 `ss -lnt` 應顯示後端為 `127.0.0.1:8080`；外網 `http://YOUR_PUBLIC_IP:8080` 不可連線。

VPS 日誌與維護：

```bash
sudo journalctl -u localktv -n 100 --no-pager
sudo tail -n 50 /var/log/nginx/error.log
sudo /snap/bin/certbot certificates
sudo systemctl restart localktv
```

### 1 GB VPS 的記憶體保護

Oracle 免費機只有約 1 GB RAM；沒有 swap 時，下載／轉檔尖峰會讓 kernel 的 OOM killer 直接殺掉 LocalKTV。
建議加 2 GB swap、讓 LocalKTV 最後才被殺，並限制 journal 大小：

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/60-localktv.conf && sudo sysctl --system
sudo install -d /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' | sudo tee /etc/systemd/journald.conf.d/60-localktv.conf
sudo systemctl restart systemd-journald
```

`deploy/localktv.service` 已含 `OOMScoreAdjust=-500` 與 `Restart=always`；舊安裝重新 `install` 該檔後
`sudo systemctl daemon-reload && sudo systemctl restart localktv`。

### AI 人聲分離（選用）

裝好後，點歌會自動走 AI 伴奏；所有包廂閒置 5 分鐘後，伺服器會一首一首補做已快取的歌，有人唱歌就暫停。
分離程式記憶體約 300 MB，優先序最低，記憶體不夠時 kernel 會先砍它，不會砍 LocalKTV。每首歌多約 12 MB（兩個 AAC 192k 的 .m4a）。

```bash
# 1. 在 PC 上準備模型（要 onnx 套件，只有這一步用到）
pip install onnx
python server/fetch_model.py          # 產生 models/UVR_MDXNET_KARA_2.dyn.onnx
scp models/UVR_MDXNET_KARA_2.dyn.onnx ubuntu@YOUR_PUBLIC_IP:/home/ubuntu/

# 2. VPS：裝 onnxruntime、放模型、打開 AI
sudo /opt/localktv/.venv/bin/pip install -r /opt/localktv/server/requirements-ai.txt
sudo install -o localktv -g localktv -m 644 /home/ubuntu/UVR_MDXNET_KARA_2.dyn.onnx /opt/localktv/models/
sudo sed -i 's/^KTV_AI_ENABLED=.*/KTV_AI_ENABLED=1/' /etc/localktv.env
sudo install -m 644 /opt/localktv/deploy/localktv.service /etc/systemd/system/localktv.service   # 含 OOMPolicy=continue
sudo systemctl daemon-reload && sudo systemctl restart localktv

# 3. 確認
sudo journalctl -u localktv -f | grep -i "AI \|separat"
```

日誌裡會看到 `AI separating <歌> (backfill)` 與 `separated <歌> in 230s`，接著 `pitch <歌>: notes N`（評分用的標準音 `pitch.json`，幾秒鐘；以前分離好的歌會在閒置時補做）。評分不用另外裝東西，但電視頁要開在 HTTPS 上才能用麥克風。要關掉補做：`/etc/localktv.env` 加 `KTV_AI_BACKFILL=0`；整個關掉 AI：`KTV_AI_ENABLED=0`。

2026-10-23 以前分離的歌是 FLAC（一首約 47 MB），伺服器照樣能播。轉成 AAC 省空間（一首約 13 MB，伺服器開著也能轉，一首幾秒）：

```bash
sudo -u localktv KTV_DATA_DIR=/var/lib/localktv /opt/localktv/.venv/bin/python /opt/localktv/server/convert_stems.py          # 先看有幾首
sudo -u localktv KTV_DATA_DIR=/var/lib/localktv /opt/localktv/.venv/bin/python /opt/localktv/server/convert_stems.py --write  # FLAC 移到 /var/lib/localktv/stems-flac-backup/
# 聽過沒問題再刪備份：sudo rm -r /var/lib/localktv/stems-flac-backup
```

### 連不上時怎麼查

先分清楚是「整台 VPS 掛了」、「Nginx 正常但 LocalKTV 沒回應」，還是「LocalKTV 被重啟」。

```bash
# 1. VPS 有沒有重開機、開了多久（看得到上一次開機的紀錄＝journal 有持久化）
uptime; last -x reboot shutdown | head; journalctl --list-boots | tail -5

# 2. LocalKTV 現況與出事前後的紀錄（時間用 UTC，換成實際時段）
systemctl status localktv --no-pager
sudo journalctl -u localktv --since "2026-10-18 13:30" --until "2026-10-18 13:50" --no-pager
sudo journalctl -u localktv --since today --no-pager | grep -E "WARNING|ERROR"

# 3. 是不是記憶體不夠被 OOM killer 殺掉（加 -b -1 看上一次開機）
sudo journalctl -k --no-pager | grep -iE "oom|killed process"
free -m; swapon --show
sudo journalctl -u localktv --since today --no-pager | grep "ktv.monitor: stats"

# 4. Nginx 那一端：connection refused＝後端沒在跑；upstream timed out＝後端卡住
sudo tail -n 50 /var/log/nginx/error.log

# 5. 憑證（IP 憑證約六天到期，續期失敗手機會顯示憑證錯誤）
sudo /snap/bin/certbot certificates
```

LocalKTV 的 log 每行是 `時間 等級 模組: 訊息`，常見的線索：

| 訊息 | 意思 |
|---|---|
| `ktv.monitor: stats rss=… mem_available=…` | 每 5 分鐘一次；`mem_available` 低於 100MB 就危險 |
| `ktv.monitor: slow request … took …s` | 某個 HTTP 請求超過 2 秒才回應 |
| `ktv.monitor: event loop blocked for …s` | 整個伺服器卡住了（所有手機、電視同時沒反應） |
| `ktv.pipeline: prepare failed …` | 這首歌所有音源都下載失敗，後面附 traceback |
| `ktv.app: ws #… connected / disconnected` | 手機或電視連線、斷線（角色、IP、目前連線數） |
| `ktv.auth: admin login failed from …` | 管理員密碼打錯（或有人在猜） |

要看更細的紀錄（每個音源步驟為什麼失敗）：`/etc/localktv.env` 加 `KTV_LOG_LEVEL=DEBUG`，
`sudo systemctl restart localktv`；查完改回 `INFO`，避免 journal 長太快。

更新：先重新建置、封裝上傳；停止 `localktv` 後解壓新程式至 `/opt/localktv`，
更新 Python dependencies，再啟動。歌曲資料放在 `/var/lib/localktv`，請定期備份並檢查磁碟用量。
供應商 cookies 只放 `/etc/localktv.env`；請勿提交到 Git。

## 本機 LAN 與開發

`KTV_AUTH_REQUIRED=0` 且未設定 `KTV_ACCESS_KEY` 時保留原有 LAN 無登入模式。
本機 HTTP 測試登入可設定 `KTV_AUTH_REQUIRED=1`、`KTV_PUBLIC_URL=http://localhost:8080`、`KTV_COOKIE_SECURE=0`，
第一次開 `http://localhost:8080/` 會進首次設定，設定碼在啟動視窗裡；這個模式不適合公網。
使用 Vite :5173 測試登入時，PUBLIC_URL 應為 `http://localhost:5173`，
並直接開 `http://localhost:5173/login`；Vite 已代理 `/login`、`/auth` 至後端。

防護針對沒有管理員密碼、邀請 QR 或配對的人的存取與暴力猜測，不能阻止網站被掃描，也不是 DDoS 防護。
不要把管理員密碼或 `/admin` 上的邀請連結交給不可信任的人。
