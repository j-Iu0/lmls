import type { ButtonHTMLAttributes, ReactNode } from 'react';
import type { Field, Value } from '../domain/model';

function StructuredValue(
  { value, disabled, onChange }: { value: Value; disabled: boolean; onChange: (v: Value) => void },
) {
  if (value && typeof value === 'object') {
    return (
      <div className='structured-fields'>
        {Object.entries(value).map(([key, child]) => (
          <div key={key}>
            <FieldControl
              field={{
                key,
                label: key,
                type: typeof child === 'object' && child !== null
                  ? 'structured'
                  : typeof child === 'boolean'
                  ? 'toggle'
                  : typeof child === 'number'
                  ? 'number'
                  : 'text',
                default: child,
              }}
              value={child}
              disabled={disabled}
              onChange={(v) => {
                const next = structuredClone(value);
                if (Array.isArray(next)) next[Number(key)] = v;
                else next[key] = v;
                onChange(next);
              }}
            />
          </div>
        ))}
      </div>
    );
  }
  return null;
}

export function IconButton(
  { label, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
    label: string;
    children: ReactNode;
  },
) {
  return (
    <button type='button' className='icon-button' aria-label={label} title={label} {...props}>
      {children}
    </button>
  );
}
export function FieldControl(
  { field, value, disabled, onChange }: {
    field: Field;
    value: Value;
    disabled: boolean;
    onChange: (value: Value) => void;
  },
) {
  const current = value === undefined ? field.default : value;
  if (field.type === 'structured') {
    return (
      <div className='field'>
        <span>{field.label}</span>
        <StructuredValue value={current} disabled={disabled} onChange={onChange} />
      </div>
    );
  }
  return (
    <label className={`field field-${field.type} nodrag nowheel`}>
      <span>
        {field.label}
        {field.type === 'range' && (
          <output>
            {String(current ?? '')} <small>{field.unit}</small>
          </output>
        )}
      </span>
      {field.type === 'select'
        ? (
          <select
            disabled={disabled}
            value={String(current ?? '')}
            onChange={(e) => onChange(field.nullable && !e.target.value ? null : e.target.value)}
          >
            {field.nullable && <option value=''>Default</option>}
            {current !== null && !field.choices?.includes(String(current)) && (
              <option>{String(current)}</option>
            )}
            {field.choices?.map((c) => <option key={c} value={c}>{field.labels?.[c] ?? c}</option>)}
          </select>
        )
        : field.type === 'range'
        ? (
          <input
            type='range'
            disabled={disabled}
            min={field.min}
            max={field.max}
            step={field.step}
            value={Number(current)}
            onChange={(e) => onChange(Number(e.target.value))}
          />
        )
        : field.type === 'toggle'
        ? (
          <input
            role='switch'
            type='checkbox'
            disabled={disabled}
            checked={Boolean(current)}
            onChange={(e) => onChange(e.target.checked)}
          />
        )
        : (
          <input
            type={field.type === 'number'
              ? 'number'
              : /key|token|password/.test(field.key)
              ? 'password'
              : 'text'}
            step={field.step ?? 'any'}
            required={field.required}
            placeholder={field.nullable ? 'Default' : undefined}
            disabled={disabled}
            value={String(current ?? '')}
            onChange={(e) =>
              onChange(
                field.nullable && !e.target.value
                  ? null
                  : field.type === 'number'
                  ? Number(e.target.value)
                  : e.target.value,
              )}
          />
        )}
    </label>
  );
}
