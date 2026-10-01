//! HTTP endpoints of the agent. Thin layer: check the token, parse JSON, call the right module.
//! Responsible for: URL → handler mapping, X-Agent-Token check, turning results into HTTP replies.
//! NOT responsible for: any routing / release / VRRP logic (routes.rs, release.rs, vrrp.rs).
//! Serves Criteria 2 + 4 (the controller drives gateways only through these endpoints).

use crate::error::AgentError;
use crate::haproxy::Haproxy;
use crate::release::{ConfigRequest, ConfigResult, ReleaseManager};
use crate::routes::{RouteManager, RoutesRequest};
use crate::vrrp;
use axum::extract::{Request, State};
use axum::http::StatusCode;
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;

pub struct AppState {
    pub token: String,
    pub gw_name: String,
    pub vrrp_state_file: PathBuf,
    pub force_backup_file: PathBuf,
    pub haproxy: Arc<dyn Haproxy>,
    pub routes: RouteManager,
    pub releases: ReleaseManager,
}

type Shared = State<Arc<AppState>>;

pub fn router(state: Arc<AppState>) -> Router {
    let protected = Router::new()
        .route("/status", get(status))
        .route("/routes", put(put_routes))
        .route("/config", post(post_config))
        .route("/config/rollback", post(post_config_rollback))
        .route("/failover", post(post_failover))
        .route("/failover/clear", post(post_failover_clear))
        .layer(middleware::from_fn_with_state(state.clone(), require_token));
    Router::new().route("/healthz", get(|| async { "ok" })).merge(protected).with_state(state)
}

async fn require_token(State(state): Shared, request: Request, next: Next) -> Response {
    let sent = request.headers().get("x-agent-token").and_then(|v| v.to_str().ok());
    if sent != Some(state.token.as_str()) {
        return AgentError::Unauthorized.into_response();
    }
    next.run(request).await
}

async fn status(State(state): Shared) -> Json<Value> {
    let releases = state.releases.state();
    Json(json!({
        "gw_name": state.gw_name,
        "vrrp_state": vrrp::read_state(&state.vrrp_state_file),
        "haproxy_running": state.haproxy.is_running().await,
        "config_version": releases.active,
        "previous_config_version": releases.previous,
        "routes_version": state.routes.version(),
        "forced_backup": vrrp::is_forced_backup(&state.force_backup_file),
        "last_config_apply": state.releases.last_apply(),
        "last_routes_apply": state.routes.last_apply(),
    }))
}

async fn put_routes(State(state): Shared, Json(request): Json<RoutesRequest>) -> Response {
    let result = state.routes.apply(request).await;
    (result.outcome.status_code(), Json(result)).into_response()
}

async fn post_config(State(state): Shared, Json(request): Json<ConfigRequest>) -> Result<Response, AgentError> {
    Ok(config_reply(state.releases.apply(request).await?))
}

async fn post_config_rollback(State(state): Shared) -> Result<Response, AgentError> {
    Ok(config_reply(state.releases.rollback_to_previous().await?))
}

fn config_reply(result: ConfigResult) -> Response {
    let code = result.outcome.map(|o| o.status_code()).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (code, Json(result)).into_response()
}

async fn post_failover(State(state): Shared) -> Result<Json<Value>, AgentError> {
    vrrp::force_backup(&state.force_backup_file)?;
    tracing::warn!("forced to BACKUP by controller");
    Ok(Json(json!({ "forced_backup": true })))
}

async fn post_failover_clear(State(state): Shared) -> Result<Json<Value>, AgentError> {
    vrrp::clear_force_backup(&state.force_backup_file)?;
    tracing::info!("force-backup cleared");
    Ok(Json(json!({ "forced_backup": false })))
}
