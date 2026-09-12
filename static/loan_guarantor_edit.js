(() => {
  const modal = document.getElementById('guarantorEditModal');
  if (!modal) return;
  const form = document.getElementById('guarantorEditForm');
  const rows = document.getElementById('guarantorEditRows');
  const error = document.getElementById('guarantorEditError');
  const save = document.getElementById('guarantorSave');
  const add = document.getElementById('guarantorAddDetail');
  let details = JSON.parse(document.getElementById('guarantorInitialDetails').textContent);
  let saving = false;
  let nextId = 0;

  function addRow(detail = {}, focus = false) {
    const row = document.createElement('div');
    row.className = 'row g-2 border rounded-3 p-2 m-0';
    const id = ++nextId;
    for (const [key, title, limit, width] of [['name', 'Detail name', 100, 'col-md-4'], ['value', 'Detail value', 500, 'col-md-8']]) {
      const column = document.createElement('div');
      column.className = `col-12 ${width}`;
      const label = document.createElement('label');
      label.className = 'form-label';
      label.htmlFor = `guarantor-${id}-${key}`;
      label.textContent = title;
      const input = document.createElement('input');
      input.className = 'form-control';
      input.id = label.htmlFor;
      input.name = `guarantor_${key}[]`;
      input.maxLength = limit;
      input.required = true;
      input.value = detail[key] || '';
      column.append(label, input);
      row.append(column);
    }
    if (!detail.name) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn btn-outline-secondary col-auto mt-2 ms-2';
      remove.textContent = 'Remove new detail';
      remove.addEventListener('click', () => {
        row.remove();
        add.disabled = false;
        add.focus();
      });
      row.append(remove);
    }
    rows.append(row);
    add.disabled = rows.children.length >= 50;
    if (focus) row.querySelector('input').focus();
  }

  modal.addEventListener('show.bs.modal', () => {
    rows.replaceChildren();
    error.hidden = true;
    (details.length ? details : [{}]).forEach(detail => addRow(detail));
  });
  modal.addEventListener('shown.bs.modal', () => rows.querySelector('input')?.focus());
  modal.addEventListener('hide.bs.modal', event => { if (saving) event.preventDefault(); });
  add.addEventListener('click', () => { if (rows.children.length < 50) addRow({}, true); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving || !form.reportValidity()) return;
    const body = new FormData(form);
    saving = true;
    error.hidden = true;
    form.querySelectorAll('button, input').forEach(control => { control.disabled = true; });
    save.textContent = 'Saving...';
    try {
      const response = await fetch(form.action, { method: 'POST', body, credentials: 'same-origin', headers: { Accept: 'application/json' } });
      const result = await response.json().catch(() => { throw new Error('Unable to save. Please reload the page and try again.'); });
      if (!response.ok || !result.ok) throw new Error(result.error || 'Unable to save guarantor details.');
      details = result.details;
      const section = document.getElementById('guarantorDetails');
      section.replaceChildren();
      details.forEach(detail => {
        const row = document.createElement('div');
        row.className = 'border-bottom py-2';
        const label = document.createElement('small');
        label.className = 'text-muted';
        label.textContent = detail.name;
        const value = document.createElement('div');
        value.textContent = detail.value;
        row.append(label, value);
        section.append(row);
      });
      saving = false;
      bootstrap.Modal.getInstance(modal).hide();
      document.getElementById('guarantorSaveStatus').textContent = 'Guarantor details saved.';
    } catch (err) {
      error.textContent = err.message || 'Unable to save guarantor details. Please try again.';
      error.hidden = false;
      error.scrollIntoView({ block: 'nearest' });
    } finally {
      saving = false;
      form.querySelectorAll('button, input').forEach(control => { control.disabled = false; });
      add.disabled = rows.children.length >= 50;
      save.textContent = 'Save changes';
    }
  });
})();
