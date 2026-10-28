# Installation (Debian 13 "trixie")

Until the Debian package exists (docs/ROADMAP.md phase 2), by hand:

```sh
apt install nginx gunicorn python3-django python3-segno python3-argon2 \
	python3-cryptography gettext
# PostgreSQL (optional; SQLite otherwise)
apt install postgresql python3-psycopg

adduser --system --group --home /var/lib/activitytrack-web activitytrack-web
git clone https://github.com/zipclaude/activitytrack-web /usr/share/activitytrack-web
install -d -m 0750 -o root -g activitytrack-web /etc/activitytrack-web
install -m 0640 -g activitytrack-web deploy/settings.ini.example /etc/activitytrack-web/settings.ini
editor /etc/activitytrack-web/settings.ini
```

PostgreSQL with peer authentication:

```sh
su postgres -c "createuser activitytrack-web"
su postgres -c "createdb -O activitytrack-web activitytrack"
```

Database, static files, organisation and first administrator:

```sh
cd /usr/share/activitytrack-web/server
export ATWEB_CONFIG=/etc/activitytrack-web/settings.ini
runuser -u activitytrack-web -- python3 manage.py migrate
runuser -u activitytrack-web -- python3 manage.py collectstatic --noinput
runuser -u activitytrack-web -- python3 manage.py setup --org "My company" --admin admin
```

Services and web server:

```sh
cp deploy/activitytrack-web.service deploy/activitytrack-web.socket /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now activitytrack-web.socket
cp deploy/nginx-site.conf /etc/nginx/sites-available/activitytrack-web
editor /etc/nginx/sites-available/activitytrack-web
ln -s ../sites-available/activitytrack-web /etc/nginx/sites-enabled/
apt install python3-certbot-nginx && certbot --nginx -d at.example.com
systemctl reload nginx
```

## Self-signed certificate

Phones do not trust it: put its key's fingerprint in `[server] tls_pin`,
and the pairing QR code makes the app pin it:

```sh
openssl x509 -in cert.pem -pubkey -noout | openssl pkey -pubin -outform der \
	| openssl dgst -sha256 -binary | base64
```
