//! gw-agent entry point: read settings, build the modules, start the HTTP server.
//! Responsible for: wiring only (logging, RealHaproxy, RouteManager, ReleaseManager, router).
//! NOT responsible for: any business logic — see routes.rs, release.rs, vrrp.rs.
//! Serves Criteria 2 + 4.

mod error;
mod haproxy;
mod http;
mod release;
mod routes;
mod settings;
mod vrrp;

use anyhow::Context;
use clap::Parser;
use std::sync::Arc;
use std::time::Duration;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let settings = settings::Settings::parse();

    let haproxy: Arc<dyn haproxy::Haproxy> = Arc::new(haproxy::RealHaproxy::new(
        settings.haproxy_bin.clone(),
        settings.socket.clone(),
        settings.reload_cmd.clone(),
        settings.probe_url.clone(),
    ));
    let allowed = routes::Cidr::parse(&settings.allowed_cidr).map_err(anyhow::Error::msg)?;
    let route_manager = routes::RouteManager::new(haproxy.clone(), settings.map_file.clone(), allowed);
    let release_manager = release::ReleaseManager::new(
        haproxy.clone(),
        release::ReleaseSettings {
            releases_dir: settings.releases_dir.clone(),
            current_link: settings.current_link.clone(),
            probe_timeout: Duration::from_millis(settings.probe_timeout_ms),
            probe_interval: Duration::from_millis(settings.probe_interval_ms),
            keep_releases: settings.keep_releases,
        },
    );

    let state = Arc::new(http::AppState {
        token: settings.token.clone(),
        gw_name: settings.gw_name.clone(),
        vrrp_state_file: settings.vrrp_state_file.clone(),
        force_backup_file: settings.force_backup_file.clone(),
        haproxy,
        routes: route_manager,
        releases: release_manager,
    });

    let listener = tokio::net::TcpListener::bind(&settings.listen)
        .await
        .with_context(|| format!("cannot listen on {}", settings.listen))?;
    tracing::info!("gw-agent {} listening on {}", settings.gw_name, settings.listen);
    axum::serve(listener, http::router(state)).await?;
    Ok(())
}
