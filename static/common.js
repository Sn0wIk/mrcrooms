export const csrf = document.querySelector('meta[name="csrf-token"]').content;

export async function api(path, body, options = {}) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
    signal: AbortSignal.timeout(12000),
    ...options,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.error || `Не удалось выполнить запрос (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return result;
}

let toastTimer;
export function toast(message) {
  const el = document.querySelector('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 6500);
}

export function savedName(value) {
  try {
    if (value !== undefined) localStorage.setItem('mrc-name', value);
    return localStorage.getItem('mrc-name') || '';
  } catch { return ''; }
}
