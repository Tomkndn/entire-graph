export interface GraphNode {
  id: string;
  label: string;
  path: string;
  language: string | null;
  modified: boolean;
  in_surface: boolean;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
}

export interface GraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  modified_files: string[];
  import_surface: string[];
  repo_id: string;
  repo_root: string;
}

export interface StatusResponse {
  repo_root: string;
  repo_id: string;
  connections: number;
  entire_available: boolean;
  mappings?: number;
  db_backend?: string;
}

export interface BlastResult {
  status: string;
  import_surface: string[];
  affected_tests: string[];
  target_tests_executed: string[];
  execution_time_seconds: number;
  total_time_seconds?: number;
  summary: string;
  failure_summary?: string;
  test_source?: string;
}

/** Every event shape the server pushes over /ws. */
export type WsEvent =
  | { type: "connected"; repo_id: string }
  | { type: "blast_started"; repo_id: string; modified_files: string[] }
  | { type: "surface_detected"; modified_files: string[]; import_surface: string[] }
  | { type: "db_query"; status: "querying" }
  | {
      type: "db_query";
      status: "complete";
      affected_tests: string[];
      source: string;
    }
  | { type: "test_result"; status: string; result: BlastResult }
  | { type: "error"; error: string };

/** Visual state a single node can be in (SPEC "Node glow states"). */
export type NodeVisualState =
  | "default"
  | "modified"
  | "querying"
  | "passed"
  | "failed";
