//! The ONLY file that runs the haproxy binary, the reload command, or talks to the Runtime API socket.
//! Responsible for: the `Haproxy` trait, the real implementation, and `MockHaproxy` for tests.
//! NOT responsible for: deciding what to apply (routes.rs) or when to roll back (release.rs).
//! Serves Criterion 4 (validate / reload / probe / runtime map updates).

use async_trait::async_trait;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::process::Command;

#[async_trait]
pub trait Haproxy: Send + Sync {
    /// `haproxy -c -f <path>`: Err(stderr) when the file is not a valid config.
    async fn validate(&self, path: &Path) -> Result<(), String>;
    /// Ask the running HAProxy to re-read current.cfg (no dropped connections in master-worker mode).
    async fn reload(&self) -> Result<(), String>;
    /// Body of the probe URL (the config version the running workers serve), None if unreachable.
    async fn probe_version(&self) -> Option<String>;
    async fn is_running(&self) -> bool;
    /// Send one Runtime API command and return the raw answer.
    async fn socket(&self, command: &str) -> Result<String, String>;
}

pub struct RealHaproxy {
    bin: String,
    socket_path: PathBuf,
    reload_cmd: String,
    probe_url: String,
    http: reqwest::Client,
}

impl RealHaproxy {
    pub fn new(bin: String, socket_path: PathBuf, reload_cmd: String, probe_url: String) -> Self {
        // Short timeout: the probe loop polls every 25 ms, one slow answer must not stall it.
        let http = reqwest::Client::builder()
            .timeout(Duration::from_millis(250))
            .build()
            .unwrap_or_default();
        Self { bin, socket_path, reload_cmd, probe_url, http }
    }
}

#[async_trait]
impl Haproxy for RealHaproxy {
    async fn validate(&self, path: &Path) -> Result<(), String> {
        let output = Command::new(&self.bin)
            .arg("-c")
            .arg("-f")
            .arg(path)
            .output()
            .await
            .map_err(|e| format!("cannot run {}: {e}", self.bin))?;
        if output.status.success() {
            return Ok(());
        }
        let mut text = String::from_utf8_lossy(&output.stderr).to_string();
        text.push_str(&String::from_utf8_lossy(&output.stdout));
        Err(text.trim().to_string())
    }

    async fn reload(&self) -> Result<(), String> {
        let output = Command::new("sh")
            .arg("-c")
            .arg(&self.reload_cmd)
            .output()
            .await
            .map_err(|e| format!("cannot run reload command: {e}"))?;
        if output.status.success() {
            Ok(())
        } else {
            Err(String::from_utf8_lossy(&output.stderr).trim().to_string())
        }
    }

    async fn probe_version(&self) -> Option<String> {
        let response = self.http.get(&self.probe_url).send().await.ok()?;
        if !response.status().is_success() {
            return None;
        }
        response.text().await.ok().map(|body| body.trim().to_string())
    }

    async fn is_running(&self) -> bool {
        self.socket("show info").await.is_ok()
    }

    async fn socket(&self, command: &str) -> Result<String, String> {
        let exchange = async {
            let mut stream = UnixStream::connect(&self.socket_path).await?;
            // One command per connection: HAProxy answers then closes the socket.
            stream.write_all(format!("{command}\n").as_bytes()).await?;
            let mut answer = String::new();
            stream.read_to_string(&mut answer).await?;
            Ok::<String, std::io::Error>(answer)
        };
        match tokio::time::timeout(Duration::from_secs(2), exchange).await {
            Ok(Ok(answer)) => Ok(answer),
            Ok(Err(e)) => Err(format!("runtime API socket: {e}")),
            Err(_) => Err("runtime API socket: timeout".to_string()),
        }
    }
}

/// A fake HAProxy that behaves like the real one, used by the unit tests.
#[cfg(test)]
pub mod mock {
    use super::*;
    use crate::error::lock;
    use std::collections::{BTreeMap, HashSet};
    use std::sync::Mutex;

    pub struct MockHaproxy {
        /// reload() reads this symlink to learn which version is now served.
        pub current_link: PathBuf,
        pub served_version: Mutex<Option<String>>,
        /// Versions that pass validation but never answer the probe (a "semantic" bad config).
        pub broken_versions: Mutex<HashSet<String>>,
        /// Runtime API state.
        pub map: Mutex<BTreeMap<String, String>>,
        pub commands: Mutex<Vec<String>>,
        /// Any socket command containing this text fails.
        pub fail_socket_on: Mutex<Option<String>>,
        pub reloads: Mutex<u32>,
    }

    impl MockHaproxy {
        pub fn new(current_link: PathBuf) -> Self {
            Self {
                current_link,
                served_version: Mutex::new(None),
                broken_versions: Mutex::new(HashSet::new()),
                map: Mutex::new(BTreeMap::new()),
                commands: Mutex::new(Vec::new()),
                fail_socket_on: Mutex::new(None),
                reloads: Mutex::new(0),
            }
        }
    }

    #[async_trait]
    impl Haproxy for MockHaproxy {
        async fn validate(&self, path: &Path) -> Result<(), String> {
            let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
            if text.contains("this is not haproxy") {
                return Err("[ALERT] parsing error: unknown keyword 'this'".to_string());
            }
            Ok(())
        }

        async fn reload(&self) -> Result<(), String> {
            *lock(&self.reloads) += 1;
            let target = std::fs::read_link(&self.current_link).map_err(|e| e.to_string())?;
            let name = target.file_name().and_then(|n| n.to_str()).unwrap_or_default();
            let version = name.trim_start_matches('v').trim_end_matches(".cfg").to_string();
            *lock(&self.served_version) = Some(version);
            Ok(())
        }

        async fn probe_version(&self) -> Option<String> {
            let served = lock(&self.served_version).clone()?;
            if lock(&self.broken_versions).contains(&served) {
                return None;
            }
            Some(served)
        }

        async fn is_running(&self) -> bool {
            true
        }

        async fn socket(&self, command: &str) -> Result<String, String> {
            lock(&self.commands).push(command.to_string());
            if let Some(bad) = lock(&self.fail_socket_on).as_deref() {
                if command.contains(bad) {
                    return Err("socket failure (mock)".to_string());
                }
            }
            let words: Vec<&str> = command.split_whitespace().collect();
            let mut map = lock(&self.map);
            match words.as_slice() {
                ["add", "map", _, key, value] | ["set", "map", _, key, value] => {
                    map.insert(key.to_string(), value.to_string());
                }
                ["del", "map", _, key] => {
                    map.remove(*key);
                }
                ["clear", "map", _] => map.clear(),
                ["show", "map", _] => {
                    let lines: Vec<String> =
                        map.iter().map(|(k, v)| format!("0x0000 {k} {v}")).collect();
                    return Ok(lines.join("\n") + "\n");
                }
                _ => {}
            }
            Ok("\n".to_string())
        }
    }
}
