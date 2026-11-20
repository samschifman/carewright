import { useEffect, useId, useRef } from 'react';

export function MermaidDiagram({ definition }: { definition: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const id = useId().replace(/:/g, '');

  useEffect(() => {
    if (!containerRef.current || !definition) return;
    let cancelled = false;

    import('mermaid').then(({ default: mermaid }) => {
      if (cancelled) return;
      mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict' });
      mermaid.render(`mermaid-${id}`, definition).then(({ svg }) => {
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = svg;
        }
      }).catch(console.error);
    });

    return () => { cancelled = true; };
  }, [definition, id]);

  return <div ref={containerRef} style={{ width: '100%', overflow: 'auto' }} />;
}
