"""React + Tailwind target for design codegen (issue #155).

Templates use ``__NAME__`` as the placeholder (plain ``str.replace``) so JSX
curly braces never collide with ``str.format``.
"""

_REACT_TAILWIND_CONFIG = """\
// tailwind.config.js — theme extension for __NAME__.
// Requires tokens.css (CSS variables) to be loaded in your app.
module.exports = {
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: 'var(--color-primary)',
          hover: 'var(--color-primary-hover)',
          subtle: 'var(--color-primary-subtle)',
          text: 'var(--color-primary-text)',
        },
        surface: 'var(--color-surface)',
        raised: 'var(--color-surface-raised)',
        ink: 'var(--color-text)',
        muted: 'var(--color-text-muted)',
        faint: 'var(--color-text-faint)',
        line: 'var(--color-border)',
        success: 'var(--color-success)',
        warning: 'var(--color-warning)',
        danger: 'var(--color-danger)',
        info: 'var(--color-info)',
      },
      fontFamily: {
        display: ['var(--font-display)'],
        body: ['var(--font-body)'],
        mono: ['var(--font-mono)'],
      },
      borderRadius: {
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
        xl: 'var(--radius-xl)',
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
      },
    },
  },
};
"""

# Long Tailwind class lists are built with array joins so no source line
# exceeds the repo's 100-char limit; the emitted JSX stays readable.
_REACT_COMPONENTS = """\\
/**
 * __NAME__ — reusable React components (Tailwind + design tokens).
 *
 * Setup:
 *   1. Import './tokens.css' once in your app entry.
 *   2. Extend tailwind.config.js with the provided theme (see file).
 *   3. Import what you need:  import { Button, Card } from './components';
 *
 * Dark mode: set data-theme="dark" on <html>; the CSS variables flip.
 */
import React from 'react';

/* ---------- Button ---------- */
const buttonVariants = {
  primary: [
    'bg-[var(--color-primary)]',
    'text-[var(--color-primary-text)]',
    'hover:bg-[var(--color-primary-hover)]',
  ].join(' '),
  secondary: [
    'bg-[var(--color-surface)]',
    'text-[var(--color-text)]',
    'border border-[var(--color-border-strong)]',
    'hover:border-[var(--color-text-faint)]',
  ].join(' '),
  ghost: [
    'bg-transparent',
    'text-[var(--color-primary)]',
    'hover:bg-[var(--color-primary-subtle)]',
  ].join(' '),
  danger: [
    'bg-[var(--color-danger)]',
    'text-white',
    'hover:brightness-95',
  ].join(' '),
};
const buttonSizes = {
  sm: 'text-[13px] px-4 py-2',
  md: 'text-[15px] px-[22px] py-3',
  lg: 'text-base px-7 py-3.5',
};
const buttonBase = [
  'inline-flex items-center justify-center gap-2 font-semibold',
  'rounded-[var(--radius-md)] border border-transparent cursor-pointer',
  'transition-all duration-150 active:translate-y-px',
  'disabled:opacity-45 disabled:cursor-not-allowed',
].join(' ');

/** Primary call-to-action. `variant`: primary | secondary | ghost | danger. */
export function Button({ variant = 'primary', size = 'md', className = '', ...props }) {
  const cls = [buttonBase, buttonVariants[variant], buttonSizes[size], className]
    .filter(Boolean)
    .join(' ');
  return <button className={cls} {...props} />;
}

/* ---------- Field (label + input / textarea / select) ---------- */
const fieldInput = [
  'w-full font-normal text-[15px] text-[var(--color-text)]',
  'bg-[var(--color-surface)]',
  'border border-[var(--color-border-strong)] rounded-[var(--radius-md)]',
  'px-3.5 py-[11px]',
  'placeholder:text-[var(--color-text-faint)]',
  'focus:outline-none focus:border-[var(--color-primary)]',
  'focus:shadow-[0_0_0_3px_var(--color-primary-subtle)]',
  'transition-all duration-150',
].join(' ');

/** Labeled form control with optional hint and error state. */
export function Field({ label, hint, error, children }) {
  return (
    <label className="grid gap-1.5 text-sm font-semibold text-[var(--color-text)]">
      {label}
      {children}
      {error ? (
        <span className="text-[13px] font-normal text-[var(--color-danger)]">
          {error}
        </span>
      ) : hint ? (
        <span className="text-[13px] font-normal text-[var(--color-text-muted)]">
          {hint}
        </span>
      ) : null}
    </label>
  );
}

export function Input(props) {
  const cls = props.invalid
    ? fieldInput + ' border-[var(--color-danger)]'
    : fieldInput;
  return <input className={cls} {...props} />;
}
export function Textarea(props) {
  return <textarea className={fieldInput} rows={3} {...props} />;
}
export function Select({ children, ...props }) {
  return (
    <select className={fieldInput} {...props}>
      {children}
    </select>
  );
}

/* ---------- Badge ---------- */
const badgeVariants = {
  success: 'bg-[var(--color-success-subtle)] text-[var(--color-success)]',
  warning: 'bg-[var(--color-warning-subtle)] text-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger-subtle)] text-[var(--color-danger)]',
  info: 'bg-[var(--color-info-subtle)] text-[var(--color-info)]',
  muted: 'bg-[var(--color-surface-raised)] text-[var(--color-text-muted)]',
};
const badgeBase = [
  'inline-flex items-center text-xs font-semibold',
  'px-3 py-[5px] rounded-full whitespace-nowrap',
].join(' ');

/** Small status pill. `variant`: success | warning | danger | info | muted. */
export function Badge({ variant = 'muted', className = '', ...props }) {
  const cls = [badgeBase, badgeVariants[variant], className]
    .filter(Boolean)
    .join(' ');
  return <span className={cls} {...props} />;
}

/* ---------- Alert ---------- */
const alertVariants = {
  success: 'bg-[var(--color-success-subtle)] border-[var(--color-success)]',
  warning: 'bg-[var(--color-warning-subtle)] border-[var(--color-warning)]',
  danger: 'bg-[var(--color-danger-subtle)] border-[var(--color-danger)]',
  info: 'bg-[var(--color-info-subtle)] border-[var(--color-info)]',
};
const alertBase = [
  'flex gap-2.5 items-start p-3.5 px-[18px]',
  'rounded-[var(--radius-md)] text-sm border',
].join(' ');

/** Inline feedback banner. `variant`: success | warning | danger | info. */
export function Alert({ variant = 'info', title, children, className = '' }) {
  const cls = [alertBase, alertVariants[variant], className]
    .filter(Boolean)
    .join(' ');
  return (
    <div className={cls} role="alert">
      <div>
        <strong className="font-bold">{title} </strong>
        {children}
      </div>
    </div>
  );
}

/* ---------- Card ---------- */
export function Card({ className = '', ...props }) {
  const cls = [
    'bg-[var(--color-surface)] border border-[var(--color-border)]',
    'rounded-[var(--radius-lg)] shadow-[var(--shadow-sm)] overflow-hidden',
    className,
  ]
    .filter(Boolean)
    .join(' ');
  return <article className={cls} {...props} />;
}

export function CardBody({ className = '', ...props }) {
  return <div className={'p-[22px] ' + className} {...props} />;
}

export function CardTitle(props) {
  return (
    <h3
      className="font-display text-[19px] font-semibold tracking-tight m-0 mb-2"
      {...props}
    />
  );
}

/* ---------- Avatar ---------- */
const avatarSizes = {
  sm: 'w-7 h-7 text-[11px]',
  md: 'w-10 h-10 text-sm',
  lg: 'w-14 h-14 text-[19px]',
};
const avatarBase = [
  'inline-flex items-center justify-center rounded-full flex-none',
  'bg-[var(--color-primary)] text-[var(--color-primary-text)] font-bold',
].join(' ');

/** Initials avatar. `size`: sm | md | lg. */
export function Avatar({ size = 'md', className = '', ...props }) {
  const cls = [avatarBase, avatarSizes[size], className]
    .filter(Boolean)
    .join(' ');
  return <span className={cls} {...props} />;
}

/* ---------- Modal ---------- */
/** Centered dialog. Render conditionally; `onClose` wires backdrop + Esc. */
export function Modal({ title, children, actions, onClose }) {
  React.useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-5 bg-black/45"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className={[
          'bg-[var(--color-surface)] rounded-[var(--radius-lg)]',
          'shadow-[var(--shadow-xl)] max-w-[480px] w-full p-[26px]',
        ].join(' ')}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-display text-xl font-bold m-0 mb-2">{title}</h2>
        <div className="text-[var(--color-text-muted)] text-sm mb-5">
          {children}
        </div>
        <div className="flex gap-2.5 justify-end">{actions}</div>
      </div>
    </div>
  );
}

/* ---------- Navbar ---------- */
export function Navbar({ brand, children, right }) {
  return (
    <nav
      className={[
        'flex items-center gap-1',
        'bg-[var(--color-surface)] border border-[var(--color-border)]',
        'rounded-[var(--radius-lg)] px-4 py-2.5',
      ].join(' ')}
    >
      <span className="font-display font-extrabold mr-4">{brand}</span>
      {children}
      <span className="ml-auto">{right}</span>
    </nav>
  );
}

const navLinkBase = [
  'px-[15px] py-2 rounded-[var(--radius-md)] no-underline',
  'text-sm font-medium text-[var(--color-text-muted)]',
  'hover:text-[var(--color-text)] hover:bg-[var(--color-surface-raised)]',
].join(' ');
const navLinkActive =
  'text-[var(--color-text)] bg-[var(--color-surface-raised)] font-semibold';

export function NavLink({ active, className = '', ...props }) {
  const cls = [navLinkBase, active ? navLinkActive : '', className]
    .filter(Boolean)
    .join(' ');
  return <a className={cls} {...props} />;
}

/* ---------- Table ---------- */
export function Table({ columns, rows }) {
  return (
    <div
      className={[
        'overflow-x-auto border border-[var(--color-border)]',
        'rounded-[var(--radius-lg)]',
      ].join(' ')}
    >
      <table className="w-full border-collapse text-sm bg-[var(--color-surface)]">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                className={[
                  'text-left px-[18px] py-3 text-xs uppercase tracking-wider',
                  'text-[var(--color-text-muted)] font-semibold',
                  'border-b border-[var(--color-border)]',
                  'bg-[var(--color-surface-raised)]',
                  c.numeric ? 'text-right' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="hover:bg-[var(--color-surface-raised)]">
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={
                    'px-[18px] py-3.5 border-b border-[var(--color-border)]' +
                    (c.numeric ? ' text-right tabular-nums' : '')
                  }
                >
                  {row[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- Progress ---------- */
export function Progress({ value, className = '' }) {
  const pct = Math.max(0, Math.min(100, value));
  const wrap = [
    'h-2 rounded-full bg-[var(--color-surface-raised)] overflow-hidden',
    className,
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <div
      className={wrap}
      role="progressbar"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full bg-[var(--color-primary)] rounded-full transition-all duration-300"
        style={{ width: pct + '%' }}
      />
    </div>
  );
}

/* ---------- Toggle ---------- */
export function Toggle({ checked, onChange, label }) {
  return (
    <label className="relative inline-flex w-[46px] h-[26px] flex-none cursor-pointer">
      <input
        type="checkbox"
        className="opacity-0 w-0 h-0 peer"
        checked={checked}
        onChange={onChange}
        aria-label={label || 'Toggle'}
      />
      <span
        className={[
          'absolute inset-0 rounded-full bg-[var(--color-border-strong)]',
          'transition-colors duration-150',
          'peer-checked:bg-[var(--color-primary)]',
          'peer-focus-visible:shadow-[0_0_0_3px_var(--color-primary-subtle)]',
          "before:content-[''] before:absolute before:w-5 before:h-5",
          'before:left-[3px] before:top-[3px] before:rounded-full',
          'before:bg-white before:shadow-sm',
          'before:transition-transform before:duration-150',
          'peer-checked:before:translate-x-5',
        ].join(' ')}
      />
    </label>
  );
}
"""


def _react_tailwind_files(name: str, tokens: dict[str, object]) -> dict[str, str]:
    from jev_agent.core.design_codegen import css_variables

    files: dict[str, str] = {}
    files["tokens.css"] = (
        f"/* Design tokens for {name} — generated by Baton Design Studio.\n"
        '   Import once in your app entry. Dark mode: data-theme="dark" '
        "on <html>. */\n" + css_variables(tokens)
    )
    files["tailwind.config.js"] = _REACT_TAILWIND_CONFIG.replace("__NAME__", name)
    files["components.jsx"] = _REACT_COMPONENTS.replace("__NAME__", name)
    files["README.md"] = (
        f"# {name} — reusable elements (React + Tailwind)\n\n"
        "1. Copy `tokens.css`, `tailwind.config.js` (merge the `theme.extend`\n"
        "   block into yours), and `components.jsx` into your project.\n"
        "2. Import `./tokens.css` once in your app entry.\n"
        "3. `import { Button, Card } from './components';`\n\n"
        'Dark mode: set `data-theme="dark"` on the `<html>` element.\n'
    )
    return files
