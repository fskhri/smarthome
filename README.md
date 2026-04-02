# print-web (local)

Web lokal untuk upload **PDF/DOC/DOCX**, preview, lalu print via **CUPS**.

## Prasyarat (Armbian/Debian/Ubuntu)

Install CUPS + Avahi + LibreOffice (buat convert DOC/DOCX → PDF):

```bash
sudo apt update
sudo apt install -y cups cups-client avahi-daemon libreoffice
sudo systemctl enable --now cups avahi-daemon
sudo cupsctl --remote-any --share-printers
sudo usermod -aG lpadmin $USER
```

Kalau pakai UFW:

```bash
sudo ufw allow 8000/tcp
sudo ufw allow 631/tcp
```

## Jalankan aplikasi

```bash
cd ~/print-web
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Buka: `http://IP-SERVER:8000`

## Catatan

- Printer harus sudah ditambahkan di CUPS (biasanya via `http://IP-SERVER:631`).
- Aplikasi ini ngeprint pakai command `lp`, jadi `cups-client` harus terpasang.
