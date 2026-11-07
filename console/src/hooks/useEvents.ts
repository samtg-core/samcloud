'use client';

import { useEffect, useRef, useState, useCallback } from 'react';

export interface RegistryEvent {
  type: string;
  target: string;
  data: Record<string, unknown>;
  timestamp: number;
}

type EventHandler = (event: RegistryEvent) => void;

/**
 * Hook that connects to the registry SSE event stream via the console proxy.
 * Auth is handled by the proxy (session token).
 *
 * Usage:
 *   const { subscribe, connected } = useEvents();
 *   useEffect(() => subscribe('service.*', (e) => refetchServices()), [subscribe]);
 */
export function useEvents() {
  const [connected, setConnected] = useState(false);
  const handlersRef = useRef<Map<string, Set<EventHandler>>>(new Map());

  const connectRef = useRef(false);

  useEffect(() => {
    if (connectRef.current) return;
    connectRef.current = true;

    const controller = new AbortController();

    async function connect() {
      try {
        const res = await fetch('/api/registry?path=/api/v1/events&stream=true', {
          signal: controller.signal,
        });

        if (!res.ok || !res.body) {
          setConnected(false);
          setTimeout(connect, 5000);
          return;
        }

        setConnected(true);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const event: RegistryEvent = JSON.parse(line.slice(6));
                dispatch(event);
              } catch {
                // skip malformed
              }
            }
          }
        }

        // Stream ended — reconnect
        setConnected(false);
        setTimeout(connect, 2000);
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {
          setConnected(false);
          setTimeout(connect, 5000);
        }
      }
    }

    connect();

    return () => {
      controller.abort();
      connectRef.current = false;
      setConnected(false);
    };
  }, []);

  function dispatch(event: RegistryEvent) {
    for (const [pattern, handlers] of handlersRef.current) {
      if (matchPattern(pattern, event.type)) {
        handlers.forEach(h => h(event));
      }
    }
  }

  const subscribe = useCallback((pattern: string, handler: EventHandler) => {
    if (!handlersRef.current.has(pattern)) {
      handlersRef.current.set(pattern, new Set());
    }
    handlersRef.current.get(pattern)!.add(handler);

    return () => {
      handlersRef.current.get(pattern)?.delete(handler);
    };
  }, []);

  return { subscribe, connected };
}

function matchPattern(pattern: string, type: string): boolean {
  if (pattern === '*') return true;
  if (pattern.endsWith('.*')) {
    return type.startsWith(pattern.slice(0, -1));
  }
  return pattern === type;
}
