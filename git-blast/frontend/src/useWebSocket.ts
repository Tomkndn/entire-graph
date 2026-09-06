import { useEffect, useRef, useState } from "react";
import type { WsEvent } from "./types";

function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws`;
}

/**
 * Connects to the dashboard's /ws endpoint, auto-reconnects, and hands each
 * decoded event to `onEvent`. Returns the live connection flag.
 */
export function useWebSocket(onEvent: (event: WsEvent) => void): {
  connected: boolean;
} {
  const [connected, setConnected] = useState(false);
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      socket = new WebSocket(wsUrl());
      socket.onopen = () => setConnected(true);
      socket.onclose = () => {
        setConnected(false);
        if (!closed) retry = setTimeout(connect, 1500);
      };
      socket.onerror = () => socket?.close();
      socket.onmessage = (msg) => {
        try {
          handlerRef.current(JSON.parse(msg.data) as WsEvent);
        } catch {
          /* ignore malformed frames */
        }
      };
    };

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { connected };
}
