//! Errors and outcomes shared by every agent module.
//! Responsible for: the AgentError type, the Outcome of an apply, and mapping both to HTTP codes.
//! NOT responsible for: deciding *when* something fails (see routes.rs / release.rs).
//! Serves Criterion 4 (every apply returns a clear, machine-readable outcome).

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;
use std::sync::{Mutex, MutexGuard};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AgentError {
    #[error("missing or wrong X-Agent-Token")]
    Unauthorized,
    #[error("{0}")]
    BadRequest(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
}

impl IntoResponse for AgentError {
    fn into_response(self) -> Response {
        let status = match self {
            AgentError::Unauthorized => StatusCode::UNAUTHORIZED,
            AgentError::BadRequest(_) => StatusCode::BAD_REQUEST,
            AgentError::Io(_) => StatusCode::INTERNAL_SERVER_ERROR,
        };
        let body = serde_json::json!({ "outcome": "error", "error": self.to_string() });
        (status, Json(body)).into_response()
    }
}

/// What happened to a routes update or a config release.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Applied,
    Unchanged,
    Rejected,
    RolledBack,
}

impl Outcome {
    /// The controller only looks at the JSON body, but the HTTP code helps humans using curl.
    pub fn status_code(self) -> StatusCode {
        match self {
            Outcome::Applied | Outcome::Unchanged => StatusCode::OK,
            Outcome::Rejected => StatusCode::UNPROCESSABLE_ENTITY,
            Outcome::RolledBack => StatusCode::CONFLICT,
        }
    }
}

/// Lock a std Mutex even if a previous holder panicked (we never want the agent to stop answering).
pub fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}
