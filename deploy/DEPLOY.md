# TaleemGo AI — Azure VM Deployment Guide

Step-by-step production setup guide for running **TaleemGo AI** on an **Azure Ubuntu 24.04 LTS (x86_64)** virtual machine (`Standard_B2als_v2`, 2 vCPU, 4 GiB RAM, 30 GB disk).

All application processes bind strictly to `127.0.0.1` and run as the unprivileged system user `taleemgo`. No web ports are exposed to the public internet.

---

## 1. Connect to the Azure VM

Open PowerShell, Terminal, or WSL on your computer and connect via SSH:

```bash
ssh -i /path/to/your-azure-key.pem azureuser@<VM_PUBLIC_IP>
```

*(Replace `/path/to/your-azure-key.pem` with your local private key file and `<VM_PUBLIC_IP>` with the Azure public IP address).*

---

## 2. Get the Code onto the VM via Deploy Key

The TaleemGo AI GitHub repository is private. Set up a dedicated, read-only deploy key so your personal credentials are never stored on the server:

### A. Generate the Deploy Key on the VM

```bash
ssh-keygen -t ed25519 -C "taleemgo-azure-deploy" -f ~/.ssh/taleemgo_deploy -N ""
cat ~/.ssh/taleemgo_deploy.pub
```

### B. Add Public Key to GitHub
1. Copy the output printed above (starts with `ssh-ed25519`).
2. Go to your GitHub repository > **Settings** > **Deploy keys** > **Add deploy key**.
3. Title: `Azure VM (Read-Only)`.
4. Key: Paste the public key string.
5. Leave "Allow write access" **unchecked** and click **Add key**.

### C. Clone and Run Automated Setup

```bash
GIT_SSH_COMMAND="ssh -i ~/.ssh/taleemgo_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  git clone <YOUR_REPO_GIT_URL> /tmp/app-src

# Copy the deploy key to the permanent service directory
sudo mkdir -p /opt/taleemgo/.ssh
sudo cp ~/.ssh/taleemgo_deploy /opt/taleemgo/.ssh/deploy_key
sudo chmod 600 /opt/taleemgo/.ssh/deploy_key

# Run the idempotent setup script
sudo bash /tmp/app-src/deploy/setup.sh
```

During setup, you will be prompted for:
* **Admin WhatsApp number** (e.g. `923001234567`)
* **Google Gemini API Key** (input hidden)
* **Groq API Key** (input hidden)

> [!NOTE]
> `BRIDGE_TOKEN` (`openssl rand -hex 32`) and `TALEEMGO_SECRET_KEY` (Fernet key) are generated automatically and saved into `/opt/taleemgo/.env` with `chmod 600`. Secrets are never echoed to the screen.

Review and configure your Supabase/LMS URLs:
```bash
sudo nano /opt/taleemgo/.env
```
Ensure `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `LMS_BASE_URL` match your LMS deployment, then save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

---

## 3. First WhatsApp Login (QR over SSH)

Because WhatsApp Web requires an interactive QR code scan on first launch, run the Node bridge once in the foreground:

### A. Run Bridge in Foreground

```bash
# Ensure background bridge service is stopped
sudo systemctl stop taleemgo-bridge 2>/dev/null || true

# Run bridge interactively as user taleemgo
sudo -u taleemgo -H bash -c 'cd /opt/taleemgo/app-src/whatsapp_bridge && set -a && . /opt/taleemgo/.env && set +a && node bridge.js'
```

### B. Scan the QR Code
1. Make your terminal window wide enough and ensure you use a monospace font so the QR squares are proportional.
2. Open WhatsApp on your phone > **Settings** (or 3 dots) > **Linked devices** > **Link a device**.
3. Scan the QR code shown in your terminal.
4. When the log displays:
   ```text
   WhatsApp ready. Own WID: ...
   ```
5. Press `Ctrl+C` to stop the foreground process.

### C. Fallback: If Terminal QR Code Looks Distorted
If your SSH terminal font breaks the ASCII QR block characters:
1. Open a second terminal session to your VM while the bridge is running.
2. Render the raw QR text into a PNG image using `qrencode`:
   ```bash
   qrencode -s 8 -o /tmp/whatsapp_qr.png < /opt/taleemgo/app-src/whatsapp_bridge/last_qr.txt
   ```
3. In a terminal on your **local computer**, download the image:
   ```bash
   scp -i /path/to/your-azure-key.pem azureuser@<VM_PUBLIC_IP>:/tmp/whatsapp_qr.png ./whatsapp_qr.png
   ```
4. Open `whatsapp_qr.png` on your computer screen and scan it with your phone.

### D. Enable and Start the Background Bridge Service

```bash
sudo systemctl enable --now taleemgo-bridge
```

---

## 4. IMPORTANT: Single WhatsApp Session Only

WhatsApp Web allows an account to link to multiple devices, but two instances of the bot connecting to the **same** linked device will conflict and kick each other off.

> [!WARNING]
> **Stop any local bot instance** (on your development PC or IDE) before scanning the QR code on the Azure VM.
> Always perform a **fresh QR scan** on the VM. **Never copy the `.wwebjs_auth` folder** between different machines.

---

## 5. Existing Users & Data Migration

Choose one of two strategies for existing user data:

### Option A: Fresh Start (Recommended)
Do not migrate database files. Existing users simply send the activation phrase (`TaleemGo AI`) from their WhatsApp chat and provide their LMS credentials again to start fresh.

### Option B: Migrate Existing Users from Local PC
If you wish to preserve signed-up users and active tokens:
1. Copy the exact `TALEEMGO_SECRET_KEY` from your local `.env` into `/opt/taleemgo/.env` on the VM (tokens are encrypted with this key; a mismatch will corrupt session decryption).
2. Stop the VM services:
   ```bash
   sudo systemctl stop taleemgo-api taleemgo-bridge
   ```
3. Transfer the `data/` folder from your PC to the VM using `scp`:
   ```bash
   scp -i /path/to/your-azure-key.pem -r ./data azureuser@<VM_PUBLIC_IP>:/tmp/data
   sudo cp -r /tmp/data/* /opt/taleemgo/app-src/data/
   sudo chown -R taleemgo:taleemgo /opt/taleemgo/app-src/data
   sudo rm -rf /tmp/data
   ```
4. Restart services:
   ```bash
   sudo systemctl start taleemgo-api taleemgo-bridge
   ```

---

## 6. Verification and Health Checks

Verify that all services are online and operational:

### A. Run Dashboard Status Check

```bash
bash /opt/taleemgo/app-src/deploy/status.sh
```

Ensure:
* `taleemgo-api`: `active`
* `taleemgo-bridge`: `active`
* `backup.timer`: `active`
* `/health` returns `{ "ready": true, "wid": "...", "lid": "..." }`

### B. Functional WhatsApp Checks
1. **Self-chat test:** Send `hi` to your own WhatsApp number. You should receive the greeting or activation flow.
2. **Second-phone test:** Send `TaleemGo AI` from another phone. Verify you receive the welcome message: *"Welcome to TaleemGo AI — your LMS assistant on WhatsApp!"*.
3. **Admin LLM check:** In your WhatsApp admin self-chat, send:
   ```text
   llm test
   ```
   Verify that all configured LLM providers (`gemini`, `groq`) report status `OK` with latency.

---

## 7. Operations & Maintenance

### View Live Logs

```bash
# View live WhatsApp bridge logs
journalctl -u taleemgo-bridge -f

# View live FastAPI command router logs
journalctl -u taleemgo-api -f

# View both combined
journalctl -u taleemgo-api -u taleemgo-bridge -f
```

### Restart Services

```bash
sudo systemctl restart taleemgo-api taleemgo-bridge
```

> [!NOTE]
> On the Linux VM, `systemd` (`Restart=always` in `taleemgo-bridge.service`) automatically restarts the bridge service if Chrome crashes or exits. For local Windows development, `whatsapp_bridge/run-bridge.cmd` provides the same auto-restart behavior in a 5-second loop.

### Apply Software Updates

To pull the latest Git commits and refresh packages:

```bash
sudo bash /opt/taleemgo/app-src/deploy/update.sh
```

### Manual Backup and Restoration

Create a backup immediately:
```bash
sudo bash /opt/taleemgo/app-src/deploy/backup.sh
```
*(Automated daily backups run automatically at 03:00 UTC via `taleemgo-backup.timer`, keeping the 7 most recent archives in `/opt/taleemgo/backups/`).*

To restore from a backup:
```bash
sudo systemctl stop taleemgo-api taleemgo-bridge
sudo tar -xzf /opt/taleemgo/backups/taleemgo-backup-<TIMESTAMP>.tar.gz -C /
sudo chown -R taleemgo:taleemgo /opt/taleemgo
sudo systemctl start taleemgo-api taleemgo-bridge
```

### If WhatsApp Session Expires / Disconnects
If WhatsApp logs out from your phone:
1. Stop the bridge: `sudo systemctl stop taleemgo-bridge`
2. Re-run Step 3 (foreground login and QR scan).
3. Restart the bridge: `sudo systemctl start taleemgo-bridge`

---

## 8. Security Checklist

* [x] **SSH Keys Only:** Disable password authentication on the VM:
  ```bash
  sudo sed -i 's/^#*PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
  sudo systemctl restart ssh
  ```
* [x] **Azure Network Security Group (NSG):** In the Azure Portal, navigate to your VM's Network Settings and edit the inbound rule for port 22 (SSH) to allow connections **only from your current public IP address** (`My IP`). Remove or deny all other inbound ports.
* [x] **File Permissions:** Ensure `/opt/taleemgo/.env` is `chmod 600` and owned by `taleemgo:taleemgo`.
* [x] **No Leaked Keys:** Never paste API keys into shell histories or public chats. If an API key was ever typed in an unencrypted terminal, rotate it immediately in Google AI Studio or Groq console.
* [x] **Unattended Security Updates:** Ensure automatic security updates are active:
  ```bash
  sudo apt-get install -y unattended-upgrades
  sudo dpkg-reconfigure -plow unattended-upgrades
  ```

---

## 9. Azure Cost Management & Teardown

Azure charges student credits by the minute while a VM is allocated.

### Budget Alert
1. In the Azure Portal, search for **Cost Management + Billing**.
2. Select **Cost alerts** > **Add**.
3. Set an alert threshold (e.g. at 50% and 80% of your student credit budget) and enter your notification email.

### Stopping vs. Deleting
* **Stopping (Deallocating):** If you stop the VM via `az vm deallocate` or the Azure Portal "Stop" button, compute charges halt. Disk storage and static IP retain a minor monthly charge.
* **Complete Teardown (After One Month):**
  1. Download the latest backup to your local machine:
     ```bash
     scp -i /path/to/your-azure-key.pem azureuser@<VM_PUBLIC_IP>:/opt/taleemgo/backups/$(ssh -i /path/to/your-azure-key.pem azureuser@<VM_PUBLIC_IP> 'ls -t /opt/taleemgo/backups/ | head -n 1') ./taleemgo-final-backup.tar.gz
     ```
  2. In Azure Portal, open the **Resource Group** containing the VM.
  3. Click **Delete resource group**, type the confirmation name, and delete all resources (VM, Disk, Network Interface, Public IP, NSG, VNet).

