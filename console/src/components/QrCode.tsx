'use client';

import qrcode from 'qrcode-generator';

// A QR as plain SVG rects, dark on a white quiet zone so phone cameras read it
// against the console's dark theme.
export function QrCode({ text, size = 192 }: { text: string; size?: number }) {
  const qr = qrcode(0, 'M');
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount();
  const margin = 4;
  const cells: React.ReactElement[] = [];
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (qr.isDark(r, c)) cells.push(<rect key={`${r}-${c}`} x={c + margin} y={r + margin} width={1} height={1} />);
    }
  }
  const span = n + margin * 2;
  return (
    <svg viewBox={`0 0 ${span} ${span}`} width={size} height={size} role="img" aria-label={text}
      shapeRendering="crispEdges" className="rounded">
      <rect width={span} height={span} fill="#fff" />
      <g fill="#000">{cells}</g>
    </svg>
  );
}
