from __future__ import annotations

import datetime
from pathlib import Path
import ssl
import subprocess
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# Fallback hierarchy: <repo_root>/certs -> ~/.agy-cli-manager/certs -> <module_dir>/certs
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_USER_ROOT = Path.home() / ".agy-cli-manager"

if (_REPO_ROOT / "certs").is_dir() or (_REPO_ROOT / "pyproject.toml").is_file():
    CERTS_DIR = _REPO_ROOT / "certs"
elif _USER_ROOT.is_dir():
    CERTS_DIR = _USER_ROOT / "certs"
else:
    CERTS_DIR = Path(__file__).parent / "certs"


class CertManager:
    def __init__(self, certs_dir: Path | None = None) -> None:
        self.certs_dir = certs_dir or CERTS_DIR
        self.certs_dir.mkdir(parents=True, exist_ok=True)
        self.ca_key_path = self.certs_dir / "ca.key"
        self.ca_crt_path = self.certs_dir / "ca.crt"
        self._host_contexts: dict[str, ssl.SSLContext] = {}
        self._ensure_ca()

    def _ensure_ca(self) -> None:
        if self.ca_key_path.is_file() and self.ca_crt_path.is_file():
            self.ca_key = serialization.load_pem_private_key(
                self.ca_key_path.read_bytes(), password=None
            )
            self.ca_crt = x509.load_pem_x509_certificate(
                self.ca_crt_path.read_bytes()
            )
            return

        self.ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "AGY Local Proxy CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AGY Proxy Development"),
        ])
        self.ca_crt = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()),
                critical=False,
            )
            .sign(self.ca_key, hashes.SHA256())
        )

        self.ca_key_path.write_bytes(
            self.ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.ca_crt_path.write_bytes(
            self.ca_crt.public_bytes(serialization.Encoding.PEM)
        )

    def get_ssl_context_for_host(self, hostname: str) -> ssl.SSLContext:
        clean_host = hostname.split(":")[0]
        if clean_host in self._host_contexts:
            return self._host_contexts[clean_host]

        host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, clean_host),
        ])

        san_hosts = [
            x509.DNSName(clean_host),
            x509.DNSName("*.googleapis.com"),
            x509.DNSName("cloudcode-pa.googleapis.com"),
            x509.DNSName("daily-cloudcode-pa.googleapis.com"),
        ]

        host_crt = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self.ca_crt.subject)
            .public_key(host_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(san_hosts),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(host_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca_key.public_key()),
                critical=False,
            )
            .sign(self.ca_key, hashes.SHA256())
        )

        host_key_pem = host_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        host_crt_pem = host_crt.public_bytes(serialization.Encoding.PEM)

        key_file = self.certs_dir / f"{clean_host}.key"
        crt_file = self.certs_dir / f"{clean_host}.crt"
        key_file.write_bytes(host_key_pem)
        crt_file.write_bytes(host_crt_pem)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(crt_file), keyfile=str(key_file))
        self._host_contexts[clean_host] = ctx
        return ctx

    def get_ca_bundle_path(self) -> Path:
        bundle_path = self.certs_dir / "bundle.crt"
        if bundle_path.is_file() and bundle_path.stat().st_size > 0:
            return bundle_path

        try:
            import certifi
            base_cacert = Path(certifi.where()).read_bytes()
        except ImportError:
            import ssl
            paths = ssl.get_default_verify_paths()
            if paths.cafile and Path(paths.cafile).is_file():
                base_cacert = Path(paths.cafile).read_bytes()
            else:
                base_cacert = b""

        ca_pem = self.ca_crt_path.read_bytes() if self.ca_crt_path.is_file() else b""
        bundle_path.write_bytes(base_cacert + b"\n" + ca_pem)
        return bundle_path

    def install_ca_windows(self) -> bool:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Import-Certificate -FilePath '{self.ca_crt_path}' -CertStoreLocation Cert:\\CurrentUser\\Root | Out-Null; $?",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return "True" in res.stdout

    def install_ca_to_windows_store(self) -> bool:
        return self.install_ca_windows()

    def is_ca_installed_windows(self) -> bool:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-ChildItem Cert:\\CurrentUser\\Root | Where-Object { $_.Subject -like '*AGY Local Proxy CA*' } | Measure-Object | Select-Object -ExpandProperty Count",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        try:
            return int(res.stdout.strip()) > 0
        except ValueError:
            return False

    def remove_ca_windows(self) -> bool:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-ChildItem Cert:\\CurrentUser\\Root | Where-Object { $_.Subject -like '*AGY Local Proxy CA*' } | Remove-Item; $?",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return "True" in res.stdout
