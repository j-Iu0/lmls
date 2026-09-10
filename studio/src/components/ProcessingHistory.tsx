export function ProcessingHistory({ values, color, compact = false }: {
  values: number[];
  color: string;
  compact?: boolean;
}) {
  const ceiling = Math.max(1, Math.ceil(Math.max(0, ...values) / 10) * 10);
  const latest = values.at(-1) ?? 0;
  return (
    <figure className={`processing-history ${compact ? 'compact' : ''}`}>
      <figcaption>
        <span>Mean processing time</span>
        <span>{latest} ms latest</span>
      </figcaption>
      <div className='history-plot'>
        <div className='history-axis'>
          <span>{ceiling} ms</span>
          <span>0</span>
        </div>
        <svg
          viewBox='0 0 240 48'
          preserveAspectRatio='none'
          role='img'
          aria-label={`Mean processing duration in milliseconds, oldest to newest. ${values.length} samples. Latest ${latest} ms. Vertical scale 0 to ${ceiling} ms.`}
        >
          <path d='M0 1H240 M0 24H240 M0 47H240' stroke='#46505b' strokeWidth='.5' fill='none' />
          <polyline
            fill='none'
            stroke={color}
            strokeWidth='1.5'
            vectorEffect='non-scaling-stroke'
            points={values.map((value, i) =>
              `${i * 238 / Math.max(1, values.length - 1) + 1},${47 - value / ceiling * 46}`
            ).join(' ')}
          />
        </svg>
      </div>
      <div className='history-time'>
        <span>Older</span>
        <span>{values.length} observed updates</span>
        <span>Newest</span>
      </div>
    </figure>
  );
}
