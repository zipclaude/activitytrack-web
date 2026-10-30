# Installation

ActivityTrack Web runs on gunicorn behind nginx or Apache, with SQLite or
PostgreSQL. Three ways to install it:

| Way | Systems | Updates |
|---|---|---|
| [Package](#1-debian-and-ubuntu-the-package) | Debian 13 and 14, Ubuntu 24.04 and 26.04 | install the new `.deb` |
| [Tarball](#2-tarball-or-clone-of-the-repository) `activitytrack-web-<version>.tar.gz` | Debian, Ubuntu, RHEL / Oracle Linux 9, Arch Linux, FreeBSD 14 | unpack the new one over the old |
| [Clone](#2-tarball-or-clone-of-the-repository) of the repository | the same | `git pull` (or `git checkout v<version>`) |

The packages and the tarball are on the
[Releases](https://github.com/zipclaude/activitytrack-web/releases) page,
with their `SHA256SUMS` (`sha256sum -c --ignore-missing SHA256SUMS`).
Every release builds and tries them: the packages in their distribution,
the tarball on Oracle Linux 9, Arch and FreeBSD 14
(`.github/workflows/release.yml`).

Whatever the way, the server needs then: its settings
([§3](#3-settings-and-first-administrator)), a web server
([§4](#4-web-server)) and, for phones, an HTTPS address they reach.

## 1. Debian and Ubuntu: the package

On Ubuntu the `universe` component must be enabled (it is by default).

```sh
apt install ./activitytrack-web_0.9.1-deb13_all.deb          # Debian 13
# Debian 14: -deb14, Ubuntu 24.04: -ubuntu24.04, Ubuntu 26.04: -ubuntu26.04
apt install python3-psycopg postgresql                        # optional: PostgreSQL instead of SQLite
```

The package makes the user `activitytrack-web`, the private data folder
`/var/lib/activitytrack-web` and `/etc/activitytrack-web/settings.ini` from
the example, collects the static files and prepares the SQLite database. It
installs the systemd units without starting them: edit the settings first
(§3), then

```sh
systemctl enable --now activitytrack-web.socket
```

`activitytrack-web-manage` runs Django's `manage.py` as the service user
with the settings (`activitytrack-web-manage migrate`,
`… createsuperuser`, `… shell`). A new version: `apt install` the new
`.deb`; the database is updated and the service restarted.
`apt purge activitytrack-web` removes settings and data too.

The package keeps the tarball's layout in `/usr/share/activitytrack-web`
(`$AT` below): the examples for the web server are in `$AT/deploy/`.
The 0.9.0 packages leave out `$AT/packaging/` (#8); they need nothing
from it, `activitytrack-web-manage` is already in `/usr/bin`.

## 2. Tarball or clone of the repository

The code goes in one folder (below `$AT`), the settings in
`/etc/activitytrack-web` (FreeBSD: `/usr/local/etc/activitytrack-web`), the
data in `/var/lib/activitytrack-web` (FreeBSD: `/var/db/activitytrack-web`).

Getting the code, from the tarball:

```sh
mkdir -p "$AT" && tar -xzf activitytrack-web-0.9.1.tar.gz -C "$AT" --strip-components=1
```

or from the repository (later `git pull`, or `git checkout v<version>` for a
release):

```sh
git clone https://github.com/zipclaude/activitytrack-web "$AT"
cd "$AT" && git checkout v0.9.1
```

After an update of the code: `activitytrack-web-manage migrate`,
`activitytrack-web-manage collectstatic --noinput`, then restart the
service.

### Debian 13 / 14, Ubuntu 24.04 / 26.04

The distribution's packages, the same paths as the package (`AT=/usr/share/activitytrack-web`):

```sh
apt install gunicorn python3-django python3-segno python3-argon2 \
	python3-cryptography gettext libjs-jquery
apt install python3-psycopg postgresql                        # optional
AT=/usr/share/activitytrack-web
# get the code into $AT (above)
adduser --system --group --home /var/lib/activitytrack-web activitytrack-web
# the web server reads static/ only; files/, the database and the key stay private
chmod 0711 /var/lib/activitytrack-web
install -d -m 0750 -o root -g activitytrack-web /etc/activitytrack-web
install -m 0640 -g activitytrack-web $AT/deploy/settings.ini.example /etc/activitytrack-web/settings.ini
install -m 0755 $AT/packaging/activitytrack-web-manage /usr/local/bin/
cp $AT/deploy/activitytrack-web.service $AT/deploy/activitytrack-web.socket /etc/systemd/system/
systemctl daemon-reload
```

### RHEL / Oracle Linux 9

Django is not among the distribution's packages: a virtualenv with
`server/requirements.txt` (`AT=/opt/activitytrack-web`).

```sh
dnf install python3.12 python3.12-pip nginx
AT=/opt/activitytrack-web
# get the code into $AT (above)
useradd --system --home-dir /var/lib/activitytrack-web --shell /sbin/nologin activitytrack-web
install -d -m 0711 -o activitytrack-web -g activitytrack-web /var/lib/activitytrack-web
python3.12 -m venv $AT/venv
$AT/venv/bin/pip install -r $AT/server/requirements.txt
# PostgreSQL: dnf install postgresql-server, and
#   $AT/venv/bin/pip install -r $AT/server/requirements-postgresql.txt
install -d -m 0750 -o root -g activitytrack-web /etc/activitytrack-web
install -m 0640 -g activitytrack-web $AT/deploy/settings.ini.example /etc/activitytrack-web/settings.ini
install -m 0755 $AT/packaging/activitytrack-web-manage /usr/local/bin/
printf 'ATWEB_HOME=%s\nATWEB_PYTHON=%s\n' $AT $AT/venv/bin/python > /etc/activitytrack-web/manage.env
cp $AT/deploy/activitytrack-web.service $AT/deploy/activitytrack-web.socket /etc/systemd/system/
```

The units are written for Debian: a drop-in changes the paths and the web
server's user (`nginx`; `apache` for Apache).

```sh
mkdir -p /etc/systemd/system/activitytrack-web.service.d /etc/systemd/system/activitytrack-web.socket.d
cat > /etc/systemd/system/activitytrack-web.service.d/local.conf <<EOT
[Service]
WorkingDirectory=/opt/activitytrack-web/server
ExecStart=
ExecStart=/opt/activitytrack-web/venv/bin/gunicorn --workers 3 --threads 4 --timeout 60 atweb.wsgi
EOT
printf '[Socket]\nSocketUser=nginx\n' > /etc/systemd/system/activitytrack-web.socket.d/local.conf
systemctl daemon-reload
```

With SELinux enforcing, the web server may read the static files and talk
to gunicorn once told so:

```sh
semanage fcontext -a -t httpd_sys_content_t '/var/lib/activitytrack-web/static(/.*)?'
restorecon -R /var/lib/activitytrack-web/static
setsebool -P httpd_can_network_connect 1
firewall-cmd --permanent --add-service=https && firewall-cmd --reload
```

On RHEL nginx keeps its sites in `/etc/nginx/conf.d/*.conf`: copy
`$AT/deploy/nginx-site.conf` there.

### Arch Linux

The distribution's packages, plus `segno` (only in the AUR) in a
virtualenv that sees them (`AT=/opt/activitytrack-web`):

```sh
pacman -S python python-django python-argon2-cffi python-cryptography gunicorn nginx
pacman -S python-psycopg postgresql                           # optional
AT=/opt/activitytrack-web
# get the code into $AT (above)
useradd --system --home-dir /var/lib/activitytrack-web --shell /usr/bin/nologin activitytrack-web
install -d -m 0711 -o activitytrack-web -g activitytrack-web /var/lib/activitytrack-web
python -m venv --system-site-packages $AT/venv
$AT/venv/bin/pip install 'segno>=1.6'
install -d -m 0750 -o root -g activitytrack-web /etc/activitytrack-web
install -m 0640 -g activitytrack-web $AT/deploy/settings.ini.example /etc/activitytrack-web/settings.ini
install -m 0755 $AT/packaging/activitytrack-web-manage /usr/local/bin/
printf 'ATWEB_HOME=%s\nATWEB_PYTHON=%s\n' $AT $AT/venv/bin/python > /etc/activitytrack-web/manage.env
cp $AT/deploy/activitytrack-web.service $AT/deploy/activitytrack-web.socket /etc/systemd/system/
mkdir -p /etc/systemd/system/activitytrack-web.service.d /etc/systemd/system/activitytrack-web.socket.d
cat > /etc/systemd/system/activitytrack-web.service.d/local.conf <<EOT
[Service]
WorkingDirectory=/opt/activitytrack-web/server
ExecStart=
ExecStart=/opt/activitytrack-web/venv/bin/python -m gunicorn --workers 3 --threads 4 --timeout 60 atweb.wsgi
EOT
# nginx and Apache run as http on Arch
printf '[Socket]\nSocketUser=http\n' > /etc/systemd/system/activitytrack-web.socket.d/local.conf
systemctl daemon-reload
```

On Arch nginx has no `sites-enabled`: include the site file from
`/etc/nginx/nginx.conf` (`include /etc/nginx/sites/*.conf;` in `http`).

### FreeBSD 14

Ports for everything; no systemd: the rc.d script `deploy/freebsd/activitytrack_web`
runs gunicorn on a Unix socket for the web server (`www`). Here
`AT=/usr/local/share/activitytrack-web`, the user is `atweb`. The `py312-`
prefix follows the ports' default Python (`pkg search django52` shows it);
with another one, set `activitytrack_web_python` in `/etc/rc.conf` and
`ATWEB_PYTHON` in `manage.env` to that interpreter.

```sh
pkg install python312 py312-django52 py312-segno py312-argon2-cffi py312-cryptography py312-gunicorn nginx
pkg install py312-psycopg postgresql17-server                 # optional
AT=/usr/local/share/activitytrack-web
# get the code into $AT (above)
pw useradd atweb -d /var/db/activitytrack-web -s /usr/sbin/nologin
install -d -m 0711 -o atweb -g atweb /var/db/activitytrack-web
install -d -m 0750 -o root -g atweb /usr/local/etc/activitytrack-web
sed -e 's|/var/lib/activitytrack-web|/var/db/activitytrack-web|' $AT/deploy/settings.ini.example \
	> /usr/local/etc/activitytrack-web/settings.ini
chgrp atweb /usr/local/etc/activitytrack-web/settings.ini && chmod 0640 /usr/local/etc/activitytrack-web/settings.ini
install -m 0755 $AT/packaging/activitytrack-web-manage /usr/local/bin/
printf 'ATWEB_HOME=%s\nATWEB_USER=atweb\nATWEB_PYTHON=python3.12\nATWEB_CONFIG=/usr/local/etc/activitytrack-web/settings.ini\n' $AT \
	> /usr/local/etc/activitytrack-web/manage.env
install -m 0755 $AT/deploy/freebsd/activitytrack_web /usr/local/etc/rc.d/
sysrc activitytrack_web_enable=YES
```

`service activitytrack_web start` once the settings are done (§3). nginx
(`/usr/local/etc/nginx/nginx.conf`) as in §4, with
`proxy_pass http://unix:/var/run/activitytrack-web/gunicorn.sock;`, the
static files under `/var/db/activitytrack-web/static/` and the attachments
under `/var/db/activitytrack-web/files/`.

## 3. Settings and first administrator

`settings.ini` is the only file to edit: `base_url` (the address the phones
use, written in the pairing QR codes), `allowed_hosts`, `time_zone`, the
database and who sends the attachments (`[files] offload`, §4). The comments
in the file say what each line does.

PostgreSQL with peer authentication (Debian/Ubuntu; the same users
elsewhere):

```sh
su postgres -c "createuser activitytrack-web"
su postgres -c "createdb -O activitytrack-web activitytrack"
```

and in `settings.ini`: `[database] engine = postgresql`,
`name = activitytrack`, `user = activitytrack-web`.

Database, static files (the package does these two itself), organisation
and first administrator:

```sh
activitytrack-web-manage migrate
activitytrack-web-manage collectstatic --noinput
activitytrack-web-manage setup --org "My company" --admin admin
```

Services, with systemd:

```sh
systemctl enable --now activitytrack-web.socket
```

## 4. Web server

### Web server: nginx

```sh
apt install nginx python3-certbot-nginx
cp $AT/deploy/nginx-site.conf /etc/nginx/sites-available/activitytrack-web
editor /etc/nginx/sites-available/activitytrack-web
ln -s ../sites-available/activitytrack-web /etc/nginx/sites-enabled/
certbot --nginx -d at.example.com
systemctl reload nginx
```

In `settings.ini`: `[files] offload = nginx` (the site's internal
`/_files/` location sends the attachments).

### Web server: Apache

```sh
apt install apache2 python3-certbot-apache
# optional, for [files] offload = apache
apt install libapache2-mod-xsendfile
a2enmod ssl proxy proxy_http headers expires http2
cp $AT/deploy/apache-site.conf /etc/apache2/sites-available/activitytrack-web.conf
editor /etc/apache2/sites-available/activitytrack-web.conf
a2ensite activitytrack-web
certbot --apache -d at.example.com
systemctl reload apache2
```

In `settings.ini`: `[files] offload = apache` with mod_xsendfile, else
`none` (Django sends the attachments itself; fine for small installations).

Both web servers run as `www-data`, the owner of gunicorn's socket.

### Under a path of an existing site

For a host that already serves a site: `base_url` with the path
(`https://www.example.com/activitytrack`), `allowed_hosts` with every name
and address the server is reached by. Apache: `$AT/deploy/apache-path.conf`
included in the site's `<VirtualHost *:443>`. nginx, in the site's
`server` block:

```nginx
location /activitytrack/static/ { alias /var/lib/activitytrack-web/static/; }
location /activitytrack/_files/ { internal; alias /var/lib/activitytrack-web/files/; }
location /activitytrack/ {
	client_max_body_size 30m;
	proxy_pass http://unix:/run/activitytrack-web.sock:/;
	proxy_set_header Host $host;
	proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
	proxy_set_header X-Forwarded-Proto $scheme;
}
```

(with `[files] accel_location = /activitytrack/_files/` for nginx offload).

### Self-signed certificate

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
