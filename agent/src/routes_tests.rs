//! Tests for routes.rs (kept in their own file so routes.rs stays short).
//! Covers: invalid IP rejected, diff → correct socket commands, unchanged, socket failure restores map.
//! Uses MockHaproxy: no real HAProxy needed.
//! Serves Criterion 4a.

use super::*;
use crate::haproxy::mock::MockHaproxy;

fn setup() -> (tempfile::TempDir, Arc<MockHaproxy>, RouteManager) {
    let dir = tempfile::tempdir().unwrap();
    let map_file = dir.path().join("hosts.map");
    let mock = Arc::new(MockHaproxy::new(dir.path().join("current.cfg")));
    let manager = RouteManager::new(mock.clone(), map_file, Cidr::parse("10.20.0.0/24").unwrap());
    (dir, mock, manager)
}

fn request(version: u64, pairs: &[(&str, &str)]) -> RoutesRequest {
    let routes = pairs.iter().map(|(h, ip)| (h.to_string(), ip.to_string())).collect();
    RoutesRequest { routes_version: version, routes }
}

#[tokio::test]
async fn invalid_ip_is_rejected_and_nothing_changes() {
    let (_dir, mock, manager) = setup();
    let result = manager.apply(request(1, &[("team1.cstam.felcloud.tn", "8.8.8.8")])).await;
    assert_eq!(result.outcome, Outcome::Rejected);
    assert!(mock.commands.lock().unwrap().is_empty());
    assert_eq!(manager.version(), 0);

    let result = manager.apply(request(1, &[("Bad_Host", "10.20.0.30")])).await;
    assert_eq!(result.outcome, Outcome::Rejected);
}

#[tokio::test]
async fn diff_produces_add_set_del_commands() {
    let (dir, mock, manager) = setup();
    let first = request(1, &[("a.x.tn", "10.20.0.21"), ("b.x.tn", "10.20.0.22")]);
    assert_eq!(manager.apply(first).await.outcome, Outcome::Applied);
    mock.commands.lock().unwrap().clear();

    // a changes IP, b disappears, c is new.
    let second = request(2, &[("a.x.tn", "10.20.0.23"), ("c.x.tn", "10.20.0.24")]);
    let result = manager.apply(second).await;
    assert_eq!(result.outcome, Outcome::Applied);
    assert_eq!((result.added, result.changed, result.removed), (1, 1, 1));

    let map = dir.path().join("hosts.map").display().to_string();
    let commands = mock.commands.lock().unwrap().clone();
    assert_eq!(
        commands,
        vec![
            format!("set map {map} a.x.tn 10.20.0.23"),
            format!("add map {map} c.x.tn 10.20.0.24"),
            format!("del map {map} b.x.tn"),
            format!("show map {map}"),
        ]
    );
    let on_disk = std::fs::read_to_string(dir.path().join("hosts.map")).unwrap();
    assert_eq!(on_disk, "a.x.tn 10.20.0.23\nc.x.tn 10.20.0.24\n");
    assert_eq!(*mock.reloads.lock().unwrap(), 0, "routes must never reload HAProxy");
}

#[tokio::test]
async fn same_version_and_content_is_unchanged() {
    let (_dir, _mock, manager) = setup();
    let pairs = [("a.x.tn", "10.20.0.21")];
    assert_eq!(manager.apply(request(5, &pairs)).await.outcome, Outcome::Applied);
    assert_eq!(manager.apply(request(5, &pairs)).await.outcome, Outcome::Unchanged);
}

#[tokio::test]
async fn socket_failure_restores_previous_map() {
    let (dir, mock, manager) = setup();
    assert_eq!(manager.apply(request(1, &[("a.x.tn", "10.20.0.21")])).await.outcome, Outcome::Applied);

    *mock.fail_socket_on.lock().unwrap() = Some("b.x.tn".to_string());
    let result = manager.apply(request(2, &[("a.x.tn", "10.20.0.21"), ("b.x.tn", "10.20.0.22")])).await;
    assert_eq!(result.outcome, Outcome::RolledBack);

    let live: Vec<String> = mock.map.lock().unwrap().keys().cloned().collect();
    assert_eq!(live, vec!["a.x.tn".to_string()]);
    let on_disk = std::fs::read_to_string(dir.path().join("hosts.map")).unwrap();
    assert_eq!(on_disk, "a.x.tn 10.20.0.21\n");
    assert_eq!(manager.version(), 1);
}

#[test]
fn cidr_contains() {
    let cidr = Cidr::parse("10.20.0.0/24").unwrap();
    assert!(cidr.contains("10.20.0.199".parse().unwrap()));
    assert!(!cidr.contains("10.20.1.5".parse().unwrap()));
    assert!(Cidr::parse("10.20.0.0/33").is_err());
}
