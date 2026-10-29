# Installation (Debian 13 "trixie")

Until the Debian package exists (docs/ROADMAP.md phase 2), by hand:

```sh
apt install gunicorn python3-django python3-segno python3-argon2 \
	python3-cryptography gettext libjs-jquery
# PostgreSQL (optional; SQLite otherwise)
apt install postgresql python3-psycopg

adduser --system --group --home /var/lib/activitytrack-web activitytrack-web
# the web server reads static/ only; files/ and the database stay private
chmod 0711 /var/lib/activitytrack-web
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

Services:

```sh
cp deploy/activitytrack-web.service deploy/activitytrack-web.socket /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now activitytrack-web.socket
```

## Web server: nginx

```sh
apt install nginx python3-certbot-nginx
cp deploy/nginx-site.conf /etc/nginx/sites-available/activitytrack-web
editor /etc/nginx/sites-available/activitytrack-web
ln -s ../sites-available/activitytrack-web /etc/nginx/sites-enabled/
certbot --nginx -d at.example.com
systemctl reload nginx
```

In `settings.ini`: `[files] offload = nginx` (the site's internal
`/_files/` location sends the attachments).

## Web server: Apache

```sh
apt install apache2 python3-certbot-apache
# optional, for [files] offload = apache
apt install libapache2-mod-xsendfile
a2enmod ssl proxy proxy_http headers expires http2
cp deploy/apache-site.conf /etc/apache2/sites-available/activitytrack-web.conf
editor /etc/apache2/sites-available/activitytrack-web.conf
a2ensite activitytrack-web
certbot --apache -d at.example.com
systemctl reload apache2
```

In `settings.ini`: `[files] offload = apache` with mod_xsendfile, else
`none` (Django sends the attachments itself; fine for small installations).

Both web servers run as `www-data`, the owner of gunicorn's socket.

## Under a path of an existing site

For a host that already serves a site: `base_url` with the path
(`https://www.example.com/activitytrack`), `allowed_hosts` with every name
and address the server is reached by. Apache: `deploy/apache-path.conf`
included in the site's `<VirtualHost *:443>`. nginx, in the site's
`server` block:

```nginx
location /activitytrack/static/ { alias /var/lib/activitytrack-web/static/; }
location /activitytrack/_files/ { internal; alias /var/lib/activitytrack-web/files/; }
location /activitytrack/ {
	client_max_body_size 30m;
	proxy_pass http://unix:/run/activitytrack-web.sock:/;
	proxy_set_header Host $host;
	proxy_set_header X-Forwarded-Proto $scheme;
}
```

(with `[files] accel_location = /activitytrack/_files/` for nginx offload).

## Self-signed certificate

The certificate must be fit for a TLS server: key usage
`digitalSignature`, extended key usage `serverAuth`, not a CA, the host
names in subjectAltName. A certificate made as a CA (key usage
`keyCertSign, cRLSign` only) works with TLS 1.3 only and is refused by many
clients:

```sh
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 825 \
	-subj "/CN=at.example.com" -addext "subjectAltName=DNS:at.example.com" \
	-addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature" \
	-addext "extendedKeyUsage=serverAuth" -keyout key.pem -out cert.pem
```


Phones do not trust it: put its key's fingerprint in `[server] tls_pin`,
and the pairing QR code makes the app pin it:

```sh
openssl x509 -in cert.pem -pubkey -noout | openssl pkey -pubin -outform der \
	| openssl dgst -sha256 -binary | base64
```
