//! Criterion 4b — full HAProxy config releases: validate → swap → reload → probe → (auto) rollback.
//! Responsible for: release files on disk, the current.cfg symlink, state.json {active, previous},
//! timing every step, and pruning old releases. One apply at a time.
//! NOT responsible for: running haproxy or the reload command (haproxy.rs), team routes (routes.rs).

use crate::error::{lock, AgentError, Outcome};
use crate::haproxy::Haproxy;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::Write;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Debug, Deserialize)]
pub struct ConfigRequest {
    pub version: u64,
    pub config: String,
    pub checksum: String,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct ConfigResult {
    pub outcome: Option<Outcome>,
    pub version: u64,
    pub active_version: Option<u64>,
    pub validate_ms: u64,
    pub reload_ms: u64,
    pub probe_ms: u64,
    pub rollback_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// Saved in releases/state.json so a restarted agent knows what is live.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct ReleaseState {
    pub active: Option<u64>,
    pub previous: Option<u64>,
}

pub struct ReleaseSettings {
    pub releases_dir: PathBuf,
    pub current_link: PathBuf,
    pub probe_timeout: Duration,
    pub probe_interval: Duration,
    pub keep_releases: usize,
}

pub struct ReleaseManager {
    haproxy: Arc<dyn Haproxy>,
    settings: ReleaseSettings,
    apply_lock: tokio::sync::Mutex<()>,
    state: Mutex<ReleaseState>,
    last_apply: Mutex<Option<ConfigResult>>,
}

pub fn sha256_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

impl ReleaseManager {
    pub fn new(haproxy: Arc<dyn Haproxy>, settings: ReleaseSettings) -> Self {
        let state = load_state(&settings);
        tracing::info!("release state at startup: {state:?}");
        Self { haproxy, settings, apply_lock: Default::default(), state: Mutex::new(state), last_apply: Default::default() }
    }

    pub fn state(&self) -> ReleaseState {
        *lock(&self.state)
    }

    pub fn last_apply(&self) -> Option<ConfigResult> {
        lock(&self.last_apply).clone()
    }

    fn release_path(&self, version: u64) -> PathBuf {
        self.settings.releases_dir.join(format!("v{version}.cfg"))
    }

    pub async fn apply(&self, request: ConfigRequest) -> Result<ConfigResult, AgentError> {
        if sha256_hex(&request.config) != request.checksum {
            return Err(AgentError::BadRequest("checksum does not match config".into()));
        }
        let _one_at_a_time = self.apply_lock.lock().await;
        let state = self.state();
        let mut result = ConfigResult { version: request.version, active_version: state.active, ..Default::default() };

        if state.active == Some(request.version) {
            let active_text = std::fs::read_to_string(self.release_path(request.version)).unwrap_or_default();
            if sha256_hex(&active_text) != request.checksum {
                return Err(AgentError::BadRequest(format!("version {} is active with other content", request.version)));
            }
            result.outcome = Some(Outcome::Unchanged);
            return Ok(self.finish(result));
        }

        // 1. WRITE
        let path = self.release_path(request.version);
        write_durable(&path, &request.config)?;

        // 2. VALIDATE — a broken file never touches the running HAProxy.
        let started = Instant::now();
        let validation = self.haproxy.validate(&path).await;
        result.validate_ms = elapsed_ms(started);
        if let Err(stderr) = validation {
            std::fs::remove_file(&path)?;
            result.outcome = Some(Outcome::Rejected);
            result.error = Some(stderr);
            return Ok(self.finish(result));
        }

        // 3-5. SWAP + RELOAD + PROBE
        match self.switch_to(request.version, &mut result).await {
            Ok(()) => {
                let new_state = ReleaseState { active: Some(request.version), previous: state.active };
                self.save_state(new_state)?;
                result.outcome = Some(Outcome::Applied);
                result.active_version = new_state.active;
                self.prune(new_state);
            }
            Err(e) => {
                result.error = Some(e);
                self.roll_back(state.active, &mut result).await;
            }
        }
        Ok(self.finish(result))
    }

    /// POST /config/rollback: make the previous release active again.
    pub async fn rollback_to_previous(&self) -> Result<ConfigResult, AgentError> {
        let _one_at_a_time = self.apply_lock.lock().await;
        let state = self.state();
        let previous = state.previous.ok_or(AgentError::BadRequest("no previous release".into()))?;
        let mut result = ConfigResult { version: previous, active_version: state.active, ..Default::default() };
        match self.switch_to(previous, &mut result).await {
            Ok(()) => {
                let new_state = ReleaseState { active: Some(previous), previous: state.active };
                self.save_state(new_state)?;
                result.outcome = Some(Outcome::Applied);
                result.active_version = new_state.active;
            }
            Err(e) => {
                result.error = Some(e);
                self.roll_back(state.active, &mut result).await;
            }
        }
        Ok(self.finish(result))
    }

    /// Point current.cfg at `version`, reload, and wait until HAProxy answers that version.
    async fn switch_to(&self, version: u64, result: &mut ConfigResult) -> Result<(), String> {
        swap_symlink(&self.release_path(version), &self.settings.current_link)
            .map_err(|e| format!("swap symlink: {e}"))?;

        let started = Instant::now();
        let reload = self.haproxy.reload().await;
        result.reload_ms += elapsed_ms(started);
        reload.map_err(|e| format!("reload failed: {e}"))?;

        let started = Instant::now();
        let answered = self.wait_for_version(version).await;
        result.probe_ms += elapsed_ms(started);
        if answered {
            Ok(())
        } else {
            Err(format!("probe never answered version {version}"))
        }
    }

    async fn wait_for_version(&self, version: u64) -> bool {
        let wanted = version.to_string();
        let deadline = Instant::now() + self.settings.probe_timeout;
        while Instant::now() < deadline {
            if self.haproxy.probe_version().await.as_deref() == Some(wanted.as_str()) {
                return true;
            }
            tokio::time::sleep(self.settings.probe_interval).await;
        }
        false
    }

    /// The new release did not come up: go back to the one that was active before.
    async fn roll_back(&self, active: Option<u64>, result: &mut ConfigResult) {
        result.outcome = Some(Outcome::RolledBack);
        let Some(active) = active else {
            tracing::error!("no active release to roll back to");
            return;
        };
        let started = Instant::now();
        let mut ignored_timings = ConfigResult::default();
        if let Err(e) = self.switch_to(active, &mut ignored_timings).await {
            tracing::error!("rollback to v{active} failed: {e}");
            result.error = Some(format!("{}; rollback also failed: {e}", result.error.clone().unwrap_or_default()));
        }
        result.rollback_ms = elapsed_ms(started);
        result.active_version = Some(active);
        tracing::warn!("rolled back to v{active} in {} ms", result.rollback_ms);
    }

    fn finish(&self, result: ConfigResult) -> ConfigResult {
        tracing::info!("config v{} → {:?}", result.version, result.outcome);
        *lock(&self.last_apply) = Some(result.clone());
        result
    }

    fn save_state(&self, new_state: ReleaseState) -> Result<(), AgentError> {
        let json = serde_json::to_string(&new_state).map_err(|e| AgentError::BadRequest(e.to_string()))?;
        write_durable(&self.settings.releases_dir.join("state.json"), &json)?;
        *lock(&self.state) = new_state;
        Ok(())
    }

    /// 6. PRUNE — keep the newest `keep_releases` files, and always active + previous.
    fn prune(&self, state: ReleaseState) {
        let mut versions = list_release_versions(&self.settings.releases_dir);
        versions.sort_unstable();
        let cut = versions.len().saturating_sub(self.settings.keep_releases);
        for &version in &versions[..cut] {
            if Some(version) == state.active || Some(version) == state.previous {
                continue;
            }
            if let Err(e) = std::fs::remove_file(self.release_path(version)) {
                tracing::warn!("prune v{version}: {e}");
            }
        }
    }
}

fn elapsed_ms(started: Instant) -> u64 {
    started.elapsed().as_millis() as u64
}

/// tmp + fsync + rename: a crash never leaves a half-written file under the final name.
fn write_durable(path: &PathBuf, text: &str) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    let mut file = std::fs::File::create(&tmp)?;
    file.write_all(text.as_bytes())?;
    file.sync_all()?;
    std::fs::rename(&tmp, path)
}

/// A new symlink under a temp name, renamed over the old one: HAProxy always sees a valid link.
fn swap_symlink(target: &PathBuf, link: &PathBuf) -> std::io::Result<()> {
    let tmp = link.with_extension("cfg.tmp");
    let _ = std::fs::remove_file(&tmp);
    std::os::unix::fs::symlink(target, &tmp)?;
    std::fs::rename(&tmp, link)
}

fn list_release_versions(dir: &PathBuf) -> Vec<u64> {
    let Ok(entries) = std::fs::read_dir(dir) else { return Vec::new() };
    entries
        .filter_map(|entry| entry.ok()?.file_name().to_str().map(String::from))
        .filter_map(|name| name.strip_prefix('v')?.strip_suffix(".cfg")?.parse().ok())
        .collect()
}

/// state.json if present; otherwise trust the symlink HAProxy was started with (e.g. v0 at bootstrap).
fn load_state(settings: &ReleaseSettings) -> ReleaseState {
    let saved = std::fs::read_to_string(settings.releases_dir.join("state.json"))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok());
    if let Some(state) = saved {
        return state;
    }
    let active = std::fs::read_link(&settings.current_link)
        .ok()
        .and_then(|target| target.file_name()?.to_str().map(String::from))
        .and_then(|name| name.strip_prefix('v')?.strip_suffix(".cfg")?.parse().ok());
    ReleaseState { active, previous: None }
}

#[cfg(test)]
#[path = "release_tests.rs"]
mod tests;
