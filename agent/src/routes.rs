//! Criterion 4a — change team routes with NO HAProxy reload.
//! Responsible for: validating {host: ip}, diffing against the live map, sending add/set/del over the
//! Runtime API, saving hosts.map to disk, verifying, and restoring the old map if anything fails.
//! NOT responsible for: the HAProxy config file itself (release.rs) or talking to the socket (haproxy.rs).

use crate::error::{lock, Outcome};
use crate::haproxy::Haproxy;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::net::Ipv4Addr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Instant;

pub type RouteMap = BTreeMap<String, String>;

#[derive(Debug, Deserialize)]
pub struct RoutesRequest {
    pub routes_version: u64,
    pub routes: RouteMap,
}

#[derive(Debug, Clone, Serialize)]
pub struct RoutesResult {
    pub outcome: Outcome,
    pub routes_version: u64,
    pub added: usize,
    pub removed: usize,
    pub changed: usize,
    pub apply_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// An IPv4 network such as 10.20.0.0/24.
#[derive(Debug, Clone, Copy)]
pub struct Cidr {
    network: u32,
    mask: u32,
}

impl Cidr {
    pub fn parse(text: &str) -> Result<Cidr, String> {
        let (ip, bits) = text.split_once('/').ok_or(format!("bad CIDR {text}"))?;
        let ip: Ipv4Addr = ip.parse().map_err(|_| format!("bad CIDR {text}"))?;
        let bits: u32 = bits.parse().map_err(|_| format!("bad CIDR {text}"))?;
        if bits > 32 {
            return Err(format!("bad CIDR {text}"));
        }
        let mask = if bits == 0 { 0 } else { u32::MAX << (32 - bits) };
        Ok(Cidr { network: u32::from(ip) & mask, mask })
    }

    pub fn contains(&self, ip: Ipv4Addr) -> bool {
        u32::from(ip) & self.mask == self.network
    }
}

/// Same rule as the spec: `^[a-z0-9-]+\.[a-z0-9.-]+$`.
fn valid_hostname(host: &str) -> bool {
    let Some((first, rest)) = host.split_once('.') else { return false };
    let label_char = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-';
    !first.is_empty()
        && !rest.is_empty()
        && first.chars().all(label_char)
        && rest.chars().all(|c| label_char(c) || c == '.')
}

pub struct RouteManager {
    haproxy: Arc<dyn Haproxy>,
    map_file: PathBuf,
    allowed: Cidr,
    /// Only one update at a time; held across awaits, so it is an async lock.
    apply_lock: tokio::sync::Mutex<()>,
    /// What HAProxy currently has. Short critical sections only, so /status never waits.
    state: Mutex<(u64, RouteMap)>,
    last_apply: Mutex<Option<RoutesResult>>,
}

impl RouteManager {
    /// Starts from whatever hosts.map holds on disk (HAProxy loaded the same file).
    pub fn new(haproxy: Arc<dyn Haproxy>, map_file: PathBuf, allowed: Cidr) -> Self {
        let text = std::fs::read_to_string(&map_file).unwrap_or_default();
        let routes = parse_map_lines(&text, false);
        let state = Mutex::new((0, routes));
        Self { haproxy, map_file, allowed, apply_lock: Default::default(), state, last_apply: Default::default() }
    }

    pub fn version(&self) -> u64 {
        lock(&self.state).0
    }

    pub fn last_apply(&self) -> Option<RoutesResult> {
        lock(&self.last_apply).clone()
    }

    pub async fn apply(&self, request: RoutesRequest) -> RoutesResult {
        let _one_at_a_time = self.apply_lock.lock().await;
        let started = Instant::now();
        let mut result = RoutesResult {
            outcome: Outcome::Applied,
            routes_version: request.routes_version,
            added: 0,
            removed: 0,
            changed: 0,
            apply_ms: 0,
            error: None,
        };
        let (old_version, old_routes) = lock(&self.state).clone();

        if let Err(e) = self.validate(&request.routes) {
            result.outcome = Outcome::Rejected;
            result.error = Some(e);
        } else if old_version == request.routes_version && old_routes == request.routes {
            result.outcome = Outcome::Unchanged;
        } else {
            let commands = self.diff_commands(&old_routes, &request.routes, &mut result);
            match self.push(&commands, &request.routes).await {
                Ok(()) => *lock(&self.state) = (request.routes_version, request.routes),
                Err(e) => {
                    tracing::error!("route update failed, restoring previous map: {e}");
                    self.restore(&old_routes).await;
                    result.outcome = Outcome::RolledBack;
                    result.error = Some(e);
                }
            }
        }
        result.apply_ms = started.elapsed().as_millis() as u64;
        *lock(&self.last_apply) = Some(result.clone());
        result
    }

    fn validate(&self, routes: &RouteMap) -> Result<(), String> {
        for (host, ip) in routes {
            if !valid_hostname(host) {
                return Err(format!("invalid hostname {host:?}"));
            }
            let parsed: Ipv4Addr = ip.parse().map_err(|_| format!("invalid IPv4 {ip:?} for {host}"))?;
            if !self.allowed.contains(parsed) {
                return Err(format!("{ip} for {host} is outside the allowed network"));
            }
        }
        Ok(())
    }

    /// Turn "old map → new map" into Runtime API commands, counting each kind of change.
    fn diff_commands(&self, old: &RouteMap, new: &RouteMap, result: &mut RoutesResult) -> Vec<String> {
        let map = self.map_file.display();
        let mut commands = Vec::new();
        for (host, ip) in new {
            match old.get(host) {
                None => {
                    result.added += 1;
                    commands.push(format!("add map {map} {host} {ip}"));
                }
                Some(old_ip) if old_ip != ip => {
                    result.changed += 1;
                    commands.push(format!("set map {map} {host} {ip}"));
                }
                Some(_) => {}
            }
        }
        for host in old.keys().filter(|host| !new.contains_key(*host)) {
            result.removed += 1;
            commands.push(format!("del map {map} {host}"));
        }
        commands
    }

    /// Socket commands, then the file on disk (so a reload keeps the routes), then verify.
    async fn push(&self, commands: &[String], wanted: &RouteMap) -> Result<(), String> {
        for command in commands {
            self.map_command(command).await?;
        }
        write_map_file(&self.map_file, wanted).map_err(|e| format!("write {}: {e}", self.map_file.display()))?;
        let shown = self.haproxy.socket(&format!("show map {}", self.map_file.display())).await?;
        let live = parse_map_lines(&shown, true);
        if &live != wanted {
            return Err(format!("verification failed: HAProxy has {} routes, expected {}", live.len(), wanted.len()));
        }
        Ok(())
    }

    /// add/set/del/clear answer an empty line on success; any text is an error message.
    async fn map_command(&self, command: &str) -> Result<(), String> {
        let answer = self.haproxy.socket(command).await?;
        if answer.trim().is_empty() {
            Ok(())
        } else {
            Err(format!("`{command}` → {}", answer.trim()))
        }
    }

    /// Best effort: put back the previous file and the previous live map.
    async fn restore(&self, previous: &RouteMap) {
        if let Err(e) = write_map_file(&self.map_file, previous) {
            tracing::error!("restore map file: {e}");
        }
        let map = self.map_file.display().to_string();
        let mut commands = vec![format!("clear map {map}")];
        commands.extend(previous.iter().map(|(host, ip)| format!("add map {map} {host} {ip}")));
        for command in commands {
            if let Err(e) = self.map_command(&command).await {
                tracing::error!("restore live map: {e}");
            }
        }
    }
}

/// hosts.map lines are "host ip"; `show map` lines are "0x55d3... host ip".
fn parse_map_lines(text: &str, has_id_column: bool) -> RouteMap {
    let skip = usize::from(has_id_column);
    text.lines()
        .filter_map(|line| {
            let words: Vec<&str> = line.split_whitespace().skip(skip).collect();
            match words.as_slice() {
                [host, ip] => Some((host.to_string(), ip.to_string())),
                _ => None,
            }
        })
        .collect()
}

/// Temp file + rename: HAProxy (on reload) never reads a half-written map.
fn write_map_file(path: &PathBuf, routes: &RouteMap) -> std::io::Result<()> {
    let body: String = routes.iter().map(|(host, ip)| format!("{host} {ip}\n")).collect();
    let tmp = path.with_extension("map.tmp");
    std::fs::write(&tmp, body)?;
    std::fs::rename(&tmp, path)
}

#[cfg(test)]
#[path = "routes_tests.rs"]
mod tests;
