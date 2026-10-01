//! Agent settings, read from CLI flags or environment variables (flag wins).
//! Responsible for: names, defaults and parsing of every setting.
//! NOT responsible for: using them (main.rs wires them into the other modules).
//! Serves Criteria 2 + 4 (paths to HAProxy / Keepalived files).

use clap::Parser;
use std::path::PathBuf;

#[derive(Parser, Debug, Clone)]
#[command(name = "gw-agent", about = "CSTAM gateway agent: routes, config releases, VRRP")]
pub struct Settings {
    /// Address the agent HTTP server listens on.
    #[arg(long, env = "AGENT_LISTEN", default_value = "0.0.0.0:9000")]
    pub listen: String,

    /// Shared secret; every call except /healthz must send it as X-Agent-Token.
    #[arg(long, env = "AGENT_TOKEN")]
    pub token: String,

    /// Name of this gateway (gw1, gw2...), reported in /status.
    #[arg(long, env = "GW_NAME", default_value = "gw")]
    pub gw_name: String,

    #[arg(long, env = "HAPROXY_BIN", default_value = "haproxy")]
    pub haproxy_bin: String,

    /// HAProxy Runtime API socket (used for map updates without reload).
    #[arg(long, env = "HAPROXY_SOCKET", default_value = "/run/haproxy.sock")]
    pub socket: PathBuf,

    #[arg(long, env = "MAP_FILE", default_value = "/etc/haproxy/hosts.map")]
    pub map_file: PathBuf,

    /// One file per config version lives here: v0.cfg, v1.cfg...
    #[arg(long, env = "RELEASES_DIR", default_value = "/etc/haproxy/releases")]
    pub releases_dir: PathBuf,

    /// Symlink HAProxy is started with; we swap it atomically.
    #[arg(long, env = "CURRENT_LINK", default_value = "/etc/haproxy/current.cfg")]
    pub current_link: PathBuf,

    /// Shell command that makes HAProxy re-read current.cfg (master-worker reload).
    #[arg(long, env = "RELOAD_CMD", default_value = "kill -USR2 $(cat /run/haproxy.pid)")]
    pub reload_cmd: String,

    /// URL answering the running config version (body == version).
    #[arg(long, env = "PROBE_URL", default_value = "http://127.0.0.1/__gw_version")]
    pub probe_url: String,

    #[arg(long, env = "PROBE_TIMEOUT_MS", default_value_t = 1500)]
    pub probe_timeout_ms: u64,

    #[arg(long, env = "PROBE_INTERVAL_MS", default_value_t = 25)]
    pub probe_interval_ms: u64,

    /// Written by Keepalived's notify.sh: MASTER / BACKUP / FAULT.
    #[arg(long, env = "VRRP_STATE_FILE", default_value = "/run/gw/vrrp_state")]
    pub vrrp_state_file: PathBuf,

    /// While this file exists Keepalived lowers our priority, so the peer takes the VIP.
    #[arg(long, env = "FORCE_BACKUP_FILE", default_value = "/run/gw/force_backup")]
    pub force_backup_file: PathBuf,

    /// Routes may only point inside this network (protects against routing to anything else).
    #[arg(long, env = "ALLOWED_CIDR", default_value = "10.20.0.0/24")]
    pub allowed_cidr: String,

    /// How many old releases to keep on disk (active and previous are always kept).
    #[arg(long, env = "KEEP_RELEASES", default_value_t = 20)]
    pub keep_releases: usize,
}
