import os
import json
import math
import random

# --- LOAD TOPOLOGY FOR PER-NODE STATE GENERATION ---
_topo_path = os.path.join(os.path.dirname(__file__), '..', '..', 'command-center', 'src', 'assets', 'map_topology.json')
_topology = {"nodes": [], "links": []}
if os.path.exists(_topo_path):
    with open(_topo_path) as f:
        _topology = json.load(f)
    print(f"Loaded topology: {len(_topology['nodes'])} nodes, {len(_topology['links'])} links")

# Build quick lookup
_node_lookup = {n["id"]: n for n in _topology["nodes"]}
_link_lookup = {l["id"]: l for l in _topology["links"]}

# Build criticality and zone lookups
_node_criticality = {n["id"]: n.get("criticality", 0) for n in _topology["nodes"]}
_node_zone = {n["id"]: n.get("zone_id", "Z00") for n in _topology["nodes"]}
_zone_nodes: dict[str, list[str]] = {}
for n in _topology["nodes"]:
    z = n.get("zone_id", "Z00")
    _zone_nodes.setdefault(z, []).append(n["id"])

# Build adjacency list for BFS
_adj: dict[str, list[tuple[str, str]]] = {n["id"]: [] for n in _topology["nodes"]}
for _l in _topology["links"]:
    if _l["from"] in _adj and _l["to"] in _adj:
        _adj[_l["from"]].append((_l["to"], _l["id"]))
        _adj[_l["to"]].append((_l["from"], _l["id"]))
_sources = [n["id"] for n in _topology["nodes"] if n.get("is_source", False)]


def _resolve_targets_to_nodes(targets):
    """Convert a mixed list of node/link IDs to a set of affected node IDs."""
    resolved = set()
    for t in (targets or []):
        if t in _node_lookup:
            resolved.add(t)
        elif t in _link_lookup:
            link = _link_lookup[t]
            resolved.add(link["from"])
            resolved.add(link["to"])
    return resolved


def _get_network_reachability(closed_links, anomaly_targets, phase):
    """BFS from sources through open links. Returns (reachable_nodes, downstream_of_anomaly)."""
    reachable = set(_sources)
    queue = list(_sources)
    hop_dist: dict[str, int] = {n["id"]: 9999 for n in _topology["nodes"]}
    for s in _sources:
        hop_dist[s] = 0
        
    while queue:
        curr = queue.pop(0)
        d = hop_dist[curr]
        for nxt, lid in _adj.get(curr, []):
            if lid not in closed_links:
                if hop_dist[nxt] > d + 1:
                    hop_dist[nxt] = d + 1
                    if nxt not in reachable:
                        reachable.add(nxt)
                        queue.append(nxt)

    # Compute downstream set (nodes downstream of ALL anomaly targets)
    anomaly_nodes = _resolve_targets_to_nodes(anomaly_targets)
    downstream: set[str] = set()
    if phase in ["RUPTURE", "AI_RECOVERY"] and anomaly_nodes:
        dq = list(anomaly_nodes)
        downstream.update(anomaly_nodes)
            
        while dq:
            curr = dq.pop(0)
            d = hop_dist.get(curr, 0)
            for nxt, lid in _adj.get(curr, []):
                if lid not in closed_links and nxt not in downstream and hop_dist.get(nxt, 0) > d:
                    downstream.add(nxt)
                    dq.append(nxt)
    return reachable, downstream


def _generate_node_states(phase, step, reachable, downstream, anomaly_targets=None, sacrificed_zones=None, original_scenario="AMBIENT"):
    """Generate synthetic per-node pressure/demand based on scenario.

    anomaly_targets: list of node/link IDs representing the anomaly epicentre(s).
    sacrificed_zones: set of zone_ids being load-shed by AI during SHORTAGE triage.
    """
    anomaly_nodes = _resolve_targets_to_nodes(anomaly_targets)
    sacrificed_zones = sacrificed_zones or set()

    states = {}
    for node in _topology["nodes"]:
        nid = node["id"]
        elev = node.get("elevation", 50.0)
        base_demand = node.get("base_demand", 0.0)
        crit = _node_criticality.get(nid, 0)
        zone = _node_zone.get(nid, "Z00")

        if nid not in reachable:
            states[nid] = {
                "pressure_m": 0.0,
                "demand_lps": 0.0,
                "criticality": crit,
                "zone_id": zone,
                "status": "ISOLATED",
            }
            continue

        pressure = max(4.0, 32.0 - (elev - 27.0) * 0.45)
        pressure += 0.8 * math.sin(step * 0.3 + hash(nid) % 100 * 0.1)
        pressure += random.uniform(-0.3, 0.3)

        demand_mult = 1.0
        node_status = "NORMAL"

        if phase == "RUPTURE" and anomaly_nodes:
            if nid in anomaly_nodes:
                pressure = max(1.0, pressure - 18.0)
                node_status = "RUPTURE_EPICENTER"
            elif nid in downstream:
                pressure *= 0.25
                node_status = "DOWNSTREAM_AFFECTED"
            else:
                min_dist = _min_dist_to_targets(node, anomaly_nodes)
                if min_dist < 200:
                    pressure -= max(0.0, 8.0 - min_dist * 0.04)
                    node_status = "PROXIMITY_AFFECTED"

        elif phase == "SURGE" and anomaly_nodes:
            if nid in anomaly_nodes:
                pressure -= 12.0
                demand_mult = 4.0
                node_status = "SURGE_EPICENTER"
            else:
                min_dist = _min_dist_to_targets(node, anomaly_nodes)
                if min_dist < 300:
                    intensity = max(0.0, 1.0 - (min_dist / 300.0))
                    pressure -= 12.0 * intensity
                    demand_mult = 1.0 + 3.0 * intensity
                    node_status = "SURGE_CONE"

        elif phase == "SHORTAGE":
            # Elevation vulnerability: higher nodes lose more pressure
            elev_factor = max(0.3, 1.0 - (elev - 27.0) * 0.012)
            pressure *= 0.55 * elev_factor
            # Leaf nodes are most vulnerable
            if node.get("is_leaf", False):
                pressure *= 0.5
                node_status = "CRITICAL_VULNERABLE"
            elif elev > 65:
                node_status = "ELEVATION_VULNERABLE"
            else:
                node_status = "SUPPLY_REDUCED"

        elif phase == "AI_RECOVERY":
            if original_scenario == "RUPTURE" and anomaly_nodes:
                if nid in anomaly_nodes:
                    # AI completely isolates ruptures -> zero pressure
                    pressure = 0.0
                    node_status = "RUPTURE_EPICENTER"
                elif nid in downstream:
                    pressure = 0.0
                    node_status = "DOWNSTREAM_AFFECTED"
                else: 
                    # Surrounding nodes stabilize as pressure builds back up
                    pressure *= 0.95
                    pressure += 1.5 * math.sin(step * 0.15)
                    node_status = "AI_STABILIZED"

            elif original_scenario == "SURGE" and anomaly_nodes:
                if nid in anomaly_nodes:
                    # Surge epicenter: AI BOOSTS supply to meet demand!
                    pressure += 5.0 + 1.5 * math.sin(step * 0.15)
                    node_status = "AI_BOOSTING"
                else:
                    min_dist = _min_dist_to_targets(node, anomaly_nodes)
                    if min_dist < 400:
                        boost = max(0.0, 3.0 * (1.0 - min_dist / 400.0))
                        pressure += boost + 1.2 * math.sin(step * 0.15)
                        node_status = "AI_REROUTING"
                    else:
                        pressure *= 0.9
                        node_status = "AI_BALANCED"

            elif original_scenario == "SHORTAGE":
                # Global strategy: proportional allocation with priority weighting
                # Critical infrastructure (hospitals) gets a boost, residential gets slightly reduced
                # But NOBODY gets zero — minimum service level everywhere
                crit_weight = 1.0 + crit * 0.08  # 1.0 / 1.08 / 1.16
                pressure = pressure * 0.85 * crit_weight
                pressure += 1.5 * math.sin(step * 0.15)
                if crit >= 2:
                    node_status = "AI_PRIORITIZED"
                elif crit >= 1:
                    node_status = "AI_BALANCED"
                else:
                    node_status = "AI_STABILIZED"
            else:
                pressure *= 0.9
                node_status = "AI_STABILIZED"

        pressure = max(0.0, round(pressure, 2))
        demand = round((base_demand * 1000 * demand_mult) + random.uniform(-0.005, 0.005), 4)
        demand = max(0.0, demand)

        states[nid] = {
            "pressure_m": pressure,
            "demand_lps": round(demand, 3),
            "criticality": crit,
            "zone_id": zone,
            "status": node_status,
        }
    return states


def _min_dist_to_targets(node, target_node_ids):
    """Compute minimum Euclidean distance from a node to any target node."""
    min_d = float('inf')
    for tid in target_node_ids:
        tn = _node_lookup.get(tid)
        if tn:
            d = math.sqrt((node["x"] - tn["x"])**2 + (node["y"] - tn["y"])**2)
            if d < min_d:
                min_d = d
    return min_d


def _generate_link_states(phase, step, closed_links, reachable, downstream, anomaly_targets=None, sacrificed_zones=None, original_scenario="AMBIENT"):
    """Generate synthetic per-link flow/velocity based on scenario."""
    anomaly_nodes = _resolve_targets_to_nodes(anomaly_targets)
    anomaly_link_ids = set(t for t in (anomaly_targets or []) if t in _link_lookup)
    sacrificed_zones = sacrificed_zones or set()

    states = {}
    for link in _topology["links"]:
        lid = link["id"]

        if lid in closed_links or link["from"] not in reachable or link["to"] not in reachable:
            states[lid] = {
                "flow_lps": 0.0,
                "velocity_ms": 0.0,
            }
            continue

        diameter = link.get("diameter", 0.3)
        area = math.pi * (diameter / 2) ** 2
        base_flow = area * 1.2 * 1000  # L/s
        flow = base_flow + random.uniform(-0.05, 0.05)
        flow += 0.1 * math.sin(step * 0.2 + hash(lid) % 50 * 0.1)

        if phase == "RUPTURE" and anomaly_nodes:
            if lid in anomaly_link_ids:
                flow *= 0.15
            elif link["from"] in downstream or link["to"] in downstream:
                flow *= 0.25
        elif phase == "SURGE" and anomaly_nodes:
            from_node = _node_lookup.get(link["from"])
            to_node = _node_lookup.get(link["to"])
            if from_node and to_node:
                if link["from"] in anomaly_nodes or link["to"] in anomaly_nodes:
                    flow *= 2.5
                else:
                    avg_x = (from_node["x"] + to_node["x"]) / 2
                    avg_y = (from_node["y"] + to_node["y"]) / 2
                    min_dist = float('inf')
                    for tid in anomaly_nodes:
                        tn = _node_lookup.get(tid)
                        if tn:
                            d = math.sqrt((avg_x - tn["x"])**2 + (avg_y - tn["y"])**2)
                            if d < min_dist:
                                min_dist = d
                    if min_dist < 300:
                        intensity = max(0.0, 1.0 - (min_dist / 300.0))
                        flow *= (1.0 + 2.0 * intensity)
        elif phase == "SHORTAGE":
            flow *= 0.45
        elif phase == "AI_RECOVERY":
            if original_scenario == "RUPTURE" and anomaly_nodes:
                if lid in anomaly_link_ids or link["from"] in anomaly_nodes or link["to"] in anomaly_nodes:
                    flow = 0.0
                elif link["from"] in downstream or link["to"] in downstream:
                    flow = 0.0
                else:
                    flow *= 1.05
            elif original_scenario == "SURGE" and anomaly_nodes:
                from_node = _node_lookup.get(link["from"])
                to_node = _node_lookup.get(link["to"])
                if from_node and to_node:
                    if link["from"] in anomaly_nodes or link["to"] in anomaly_nodes:
                        flow *= 1.8  # AI boosts flow towards surge
                    else:
                        flow *= 1.1  # Partial reroute towards surge
            elif original_scenario == "SHORTAGE":
                # AI performs proportional reroute maintaining flow to critical
                from_node = _node_lookup.get(link["from"])
                to_node = _node_lookup.get(link["to"])
                # If flowing to a critical node, maintain more flow
                crit_factor = 1.0
                if to_node and to_node.get("criticality", 0) >= 2:
                    crit_factor = 1.2
                flow *= 0.85 * crit_factor
            else:
                flow *= 0.95

        flow = max(0.0, round(flow, 3))
        velocity = round(flow / (area * 1000) if area > 0 else 0.0, 2)

        states[lid] = {
            "flow_lps": flow,
            "velocity_ms": velocity,
        }
    return states


def compute_network_analytics(node_states):
    """Compute supply/demand gap, zone health, and triage recommendations."""
    if not node_states:
        return {}

    total_demand = 0.0
    total_supplied = 0.0
    zone_health: dict[str, dict] = {}
    vulnerable_zones = []

    for nid, ns in node_states.items():
        demand = ns.get("demand_lps", 0.0)
        pressure = ns.get("pressure_m", 0.0)
        zone = ns.get("zone_id", "Z00")
        crit = ns.get("criticality", 0)

        total_demand += demand
        # Simplified: supply is proportional to pressure (PDD approximation)
        if pressure > 5.0:
            total_supplied += demand
        elif pressure > 0:
            total_supplied += demand * (pressure / 5.0)

        if zone not in zone_health:
            zone_health[zone] = {
                "total_nodes": 0, "healthy": 0, "critical_nodes": 0,
                "avg_pressure": 0.0, "max_criticality": 0,
            }
        zh = zone_health[zone]
        zh["total_nodes"] += 1
        zh["avg_pressure"] += pressure
        zh["max_criticality"] = max(zh["max_criticality"], crit)
        if crit >= 2:
            zh["critical_nodes"] += 1
        if pressure > 5.0:
            zh["healthy"] += 1

    # Finalize zone averages and find sacrifice candidates
    for zid, zh in zone_health.items():
        if zh["total_nodes"] > 0:
            zh["avg_pressure"] = round(zh["avg_pressure"] / zh["total_nodes"], 2)
            zh["health_pct"] = round((zh["healthy"] / zh["total_nodes"]) * 100, 1)
        else:
            zh["health_pct"] = 0.0
        # Zones with NO critical infrastructure and low health are sacrifice candidates
        if zh["max_criticality"] < 2 and zh["health_pct"] < 60:
            vulnerable_zones.append(zid)

    supply_demand_gap = round(total_demand - total_supplied, 3) if total_demand > 0 else 0.0
    supply_pct = round((total_supplied / total_demand * 100), 1) if total_demand > 0 else 100.0

    return {
        "supply_demand_gap_lps": supply_demand_gap,
        "supply_fulfillment_pct": supply_pct,
        "zone_health": zone_health,
        "sacrifice_candidates": sorted(vulnerable_zones),
    }


def get_triage_sacrifice_zones(node_states, max_sacrifice=4):
    """Determine which zones should be load-shed during AI triage.
    Picks zones with no critical infrastructure, sorted by lowest health."""
    analytics = compute_network_analytics(node_states)
    zh = analytics.get("zone_health", {})

    candidates = []
    for zid, info in zh.items():
        if info["max_criticality"] < 2:  # Never sacrifice zones with hospitals
            candidates.append((zid, info["health_pct"], info["max_criticality"]))

    # Sort: lowest criticality first, then lowest health (most damaged)
    candidates.sort(key=lambda x: (x[2], x[1]))
    return set(c[0] for c in candidates[:max_sacrifice])
