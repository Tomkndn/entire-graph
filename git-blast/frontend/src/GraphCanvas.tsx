import { useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { layoutGraph } from "./layout";
import type { GraphEdge, GraphNode, NodeVisualState } from "./types";

interface FileNodeData extends Record<string, unknown> {
  label: string;
  language: string | null;
  visual: NodeVisualState;
  inSurface: boolean;
}

function FileNode({ data }: NodeProps<Node<FileNodeData>>) {
  return (
    <div
      className={`file-node state-${data.visual}${data.inSurface ? " in-surface" : ""}`}
    >
      <Handle type="target" position={Position.Top} />
      <div className="file-node-label">{data.label}</div>
      {data.language ? (
        <div className="file-node-lang">{data.language}</div>
      ) : null}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { file: FileNode };

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  visualState: Record<string, NodeVisualState>;
  surface: Set<string>;
}

export function GraphCanvas({ nodes, edges, visualState, surface }: Props) {
  const flowNodes = useMemo<Node<FileNodeData>[]>(() => {
    const raw: Node<FileNodeData>[] = nodes.map((n) => ({
      id: n.id,
      type: "file",
      position: { x: 0, y: 0 },
      data: {
        label: n.label,
        language: n.language,
        visual: visualState[n.id] ?? (n.modified ? "modified" : "default"),
        inSurface: surface.has(n.id) || n.in_surface,
      },
    }));
    const flowEdges: Edge[] = edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
    }));
    return layoutGraph(raw, flowEdges) as Node<FileNodeData>[];
  }, [nodes, edges, visualState, surface]);

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        animated: surface.has(e.source) && surface.has(e.target),
      })),
    [edges, surface],
  );

  return (
    <div className="graph-canvas">
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.1}
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls />
      </ReactFlow>
    </div>
  );
}
