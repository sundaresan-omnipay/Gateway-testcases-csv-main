"""
MySQL connection helper with optional SSH tunnel.

- Local: use SSH tunnel (SSH_HOST, SSH_USER, DB_HOST=RDS endpoint).
- CodeBuild: direct connection (DB_HOST, DB_PORT, no SSH vars).
"""

import os
import sys

try:
    import pymysql
except ModuleNotFoundError:  # pragma: no cover
    pymysql = None  # type: ignore


def _debug(msg: str) -> None:
    if os.environ.get("DEBUG_MYSQL") in {"1", "true", "TRUE", "yes", "YES"}:
        print(f"[mysql-debug] {msg}", file=sys.stderr)


class MySQLConnector:
    def __init__(
        self,
        db_host: str,
        db_user: str,
        db_password: str,
        db_name: str,
        ssh_host: str | None = None,
        ssh_user: str | None = None,
        db_port: int | None = None,
        ssh_pkey: str | None = None,
    ):
        self.db_host = db_host
        self.db_user = db_user
        self.db_password = db_password
        self.db_name = db_name
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.db_port = db_port
        self.ssh_pkey = ssh_pkey or os.path.expanduser("~/.ssh/id_rsa")
        self.tunnel = None
        self.connection = None
        self._connect()

    def _connect(self) -> None:
        """Establish SSH tunnel and/or database connection."""
        if pymysql is None:
            raise SystemExit("Missing 'pymysql'. Install with: pip install pymysql")

        # SSH tunnel mode: use a local forwarded port to reach the remote MySQL.
        if self.ssh_host and self.ssh_user and self.db_port is None:
            _debug(
                "Connecting via SSH tunnel. "
                f"ssh_host={self.ssh_host!r} ssh_user={self.ssh_user!r} "
                f"ssh_pkey={self.ssh_pkey!r} remote_db_host={self.db_host!r} "
                f"remote_db_port=3306 db={self.db_name!r}"
            )
            try:
                from sshtunnel import SSHTunnelForwarder
            except ModuleNotFoundError as e:  # pragma: no cover
                raise SystemExit(
                    "Missing 'sshtunnel' for SSH. Install with: pip install sshtunnel"
                ) from e

            self.tunnel = SSHTunnelForwarder(
                (self.ssh_host, 22),
                ssh_username=self.ssh_user,
                ssh_pkey=self.ssh_pkey,
                remote_bind_address=(self.db_host, 3306),
            )
            self.tunnel.start()
            _debug(f"SSH tunnel started. local_bind_port={self.tunnel.local_bind_port}")

            self.connection = pymysql.connect(
                host="127.0.0.1",
                port=self.tunnel.local_bind_port,
                user=self.db_user,
                password=self.db_password,
                database=self.db_name,
                cursorclass=pymysql.cursors.DictCursor,
            )
            _debug("MySQL connection established via tunnel.")
            return

        # Direct mode: connect directly (e.g. CodeBuild).
        if not self.ssh_host and not self.ssh_user and self.db_port is not None:
            _debug(
                "Connecting directly. "
                f"db_host={self.db_host!r} db_port={self.db_port} db={self.db_name!r} "
                f"user={self.db_user!r}"
            )
            self.connection = pymysql.connect(
                host=self.db_host,
                port=self.db_port,
                user=self.db_user,
                password=self.db_password,
                database=self.db_name,
                cursorclass=pymysql.cursors.DictCursor,
            )
            _debug("MySQL connection established directly.")
            return

        raise ValueError("Use either (SSH_HOST + SSH_USER for tunnel) or (DB_PORT for direct).")

    def close_connection(self) -> None:
        """Close database connection and stop SSH tunnel if used."""
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None

