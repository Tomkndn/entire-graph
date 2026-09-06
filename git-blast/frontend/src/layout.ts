import dagre from "@dagrejs/dagre";
import type { Edge, Node } from "@xyflow/react";

const NODE_WIDTH = 180;
const NODE_HEIGHT = 48;

/**
 * Hierarchical auto-layout with Dagre (SPEC.md "Layout"). Returns the same
 * nodes with `position` set from the computed graph, top-to-bottom.
 */
export function layoutGraph(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 70 });

  nodes.forEach((node) => {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });
  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });

  dagre.layout(g);

  return nodes.map((node) => {
    const pos = g.node(node.id);
    return {
      ...node,
      position: {
        x: (pos?.x ?? 0) - NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - NODE_HEIGHT / 2,
      },
    };
  });
}

export { NODE_WIDTH, NODE_HEIGHT };
