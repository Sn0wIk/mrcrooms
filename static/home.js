import {api, savedName} from './common.js';

document.querySelector('#name').value = savedName();
document.querySelector('#create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  const error = document.querySelector('#form-error');
  error.hidden = true;
  button.disabled = true;
  try {
    const name = document.querySelector('#name').value.trim();
    if (!name) throw new Error('Представься, чтобы друзья узнали тебя.');
    savedName(name);
    const result = await api('/api/rooms', {
      title: document.querySelector('#title').value,
      url: document.querySelector('#url').value.trim(),
    });
    location.href = result.url;
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    button.disabled = false;
  }
});
