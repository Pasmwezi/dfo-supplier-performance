(() => {
  const today = new Date().toISOString().slice(0, 10);
  const dateInput = document.querySelector('input[name="evaluation_date"]');
  if (dateInput && !dateInput.value) dateInput.value = today;

  const model = document.getElementById('model');
  const form = document.getElementById('evaluation-form');
  const existingFields = document.getElementById('existing-contract-fields');
  const newSupplierFields = document.getElementById('new-supplier-fields');
  const existingSupplierFields = document.getElementById('existing-supplier-fields');
  const newContractFields = document.getElementById('new-contract-fields');
  const sections = {
    CONSTRUCTION: document.getElementById('criteria-construction'),
    AE: document.getElementById('criteria-ae'),
    AE_CPERF: document.getElementById('criteria-ae-cperf')
  };
  function setRecordMode() {
    if (!form || !existingFields || !newSupplierFields || !existingSupplierFields || !newContractFields) return;
    const mode = form.elements.record_mode.value;
    const requiredFields = ['contract_id', 'supplier_id', 'new_supplier_name', 'new_contract_number', 'new_procurement_type', 'new_region'];
    [[existingFields, mode === 'existing'], [existingSupplierFields, mode === 'new_contract'],
     [newSupplierFields, mode === 'new'], [newContractFields, mode !== 'existing']].forEach(([container, active]) => {
      container.classList.toggle('hidden', !active);
      container.querySelectorAll('input,select').forEach(input => {
        input.disabled = !active;
        input.required = active && requiredFields.includes(input.name);
      });
    });
  }
  if (form && existingFields) {
    form.querySelectorAll('input[name="record_mode"]').forEach(input => input.addEventListener('change', setRecordMode));
    setRecordMode();
  }
  function currentInputs() { return sections[model.value].querySelectorAll('[data-score]'); }
  function notApplicable(input) {
    const checkbox = input.closest('.criterion-card')?.querySelector('[data-na-for]');
    return Boolean(checkbox?.checked);
  }
  function calculate() {
    if (!model) return;
    document.querySelectorAll('.model-construction-only').forEach(container => {
      const active = model.value === 'CONSTRUCTION';
      container.classList.toggle('hidden', !active);
      container.querySelectorAll('input').forEach(input => { input.disabled = !active; });
    });
    Object.entries(sections).forEach(([name, section]) => {
      const active = name === model.value;
      section.classList.toggle('hidden', !active);
      section.querySelectorAll('[data-na-for]').forEach(checkbox => { checkbox.disabled = !active; });
      section.querySelectorAll('[data-score]').forEach(input => {
        input.disabled = !active || notApplicable(input);
        input.required = active && !notApplicable(input);
      });
    });
    const inputs = [...currentInputs()];
    const hasNotApplicable = inputs.some(input => notApplicable(input));
    const comments = form?.elements.comments;
    if (comments) {
      comments.required = hasNotApplicable;
      comments.minLength = hasNotApplicable ? 10 : 0;
    }
    const applicableInputs = inputs.filter(input => !notApplicable(input));
    if (!applicableInputs.every(input => input.value !== '' && input.checkValidity())) {
      document.getElementById('score-preview').textContent = 'Incomplete';
      document.getElementById('outcome-preview').textContent = 'Complete all applicable criteria';
      return;
    }
    const applicableMaximum = model.value === 'CONSTRUCTION'
      ? applicableInputs.length * 20
      : applicableInputs.reduce((total, input) => total + Number(input.dataset.weight), 0);
    const earned = model.value === 'CONSTRUCTION'
      ? applicableInputs.reduce((total, input) => total + Number(input.value), 0)
      : applicableInputs.reduce((total, input) => total + Number(input.value) / 20 * Number(input.dataset.weight), 0);
    const score = earned / applicableMaximum * 100;
    const scores = applicableInputs.map(input => Number(input.value));
    const outcome = score >= 85 ? 'Congratulations' : score >= 51 ? 'Meets expectations' : score >= 30 && !scores.some(value => value <= 5) ? 'Warning' : 'Suspension recommendation';
    document.getElementById('score-preview').textContent = score.toFixed(1) + '%';
    document.getElementById('outcome-preview').textContent = outcome;
  }
  if (model) {
    model.addEventListener('change', calculate);
    document.addEventListener('input', event => { if (event.target.matches('[data-score]')) calculate(); });
    document.addEventListener('change', event => {
      if (!event.target.matches('[data-na-for]')) return;
      const score = event.target.closest('.criterion-card').querySelector('[data-score]');
      if (event.target.checked) score.value = '';
      calculate();
    });
    calculate();
  }
  if (form) form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!form.reportValidity()) return;
    const fd = new FormData(form);
    const inputs = [...currentInputs()];
    const scores = Object.fromEntries(inputs.map(input => [input.dataset.score, notApplicable(input) ? null : Number(input.value)]));
    const weights = model.value === 'CONSTRUCTION' ? null : Object.fromEntries(inputs.map(i => [i.dataset.score, Number(i.dataset.weight)]));
    const payload = {
      model: model.value, evaluator: fd.get('evaluator'),
      evaluation_date: fd.get('evaluation_date'), due_date: fd.get('due_date') || null,
      scores, weights, comments: fd.get('comments'), issues: fd.getAll('issues')
    };
    const optional = name => fd.get(name) || null;
    const optionalNumber = name => fd.get(name) === '' || fd.get(name) === null ? null : Number(fd.get(name));
    payload.project_details = {
      client_reference_number: optional('client_reference_number'), description_of_work: optional('description_of_work'),
      firm_address: optional('firm_address'), contractor_superintendent: optional('contractor_superintendent'),
      project_manager_name: optional('project_manager_name'), project_manager_telephone: optional('project_manager_telephone'),
      project_manager_fax: optional('project_manager_fax'), project_manager_cell: optional('project_manager_cell'),
      project_manager_email: optional('project_manager_email'), contract_award_amount: optionalNumber('contract_award_amount'),
      contract_award_date: optional('contract_award_date'), final_amount: optionalNumber('final_amount'),
      contract_completion_date: optional('contract_completion_date'), contract_changes_count: optionalNumber('contract_changes_count'),
      final_certificate_date: optional('final_certificate_date')
    };
    const evaluationId = form.dataset.evaluationId;
    if (evaluationId) delete payload.model;
    else if (['new', 'new_contract'].includes(fd.get('record_mode'))) {
      if (fd.get('record_mode') === 'new') {
        payload.new_supplier = {name: fd.get('new_supplier_name'), business_number: optional('new_supplier_business_number')};
      } else payload.supplier_id = Number(fd.get('supplier_id'));
      payload.new_contract = {
        contract_number: fd.get('new_contract_number'), project_number: optional('new_project_number'),
        procurement_type: fd.get('new_procurement_type'), region: fd.get('new_region'),
        department: fd.get('new_department') || 'Contracting Organization',
        standing_offer_number: optional('new_standing_offer_number'), call_up_number: optional('new_call_up_number'),
        contract_value: fd.get('new_contract_value') ? Number(fd.get('new_contract_value')) : null,
        start_date: optional('new_start_date'), end_date: optional('new_end_date'),
        status: fd.get('new_contract_status'), performance_evaluation_required: true,
        performance_regime: {CONSTRUCTION:'GI16_GC1_22',AE:'AE_EXTENDED',AE_CPERF:'GI23_GC26_2913_1'}[model.value]
      };
    } else payload.contract_id = Number(fd.get('contract_id'));
    const message = document.getElementById('form-message');
    const response = await fetch(evaluationId ? `/api/evaluations/${evaluationId}` : '/api/evaluations', {method:evaluationId ? 'PATCH' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    if (!response.ok) { const data = await response.json(); message.className='error'; message.textContent = data.detail || 'Unable to save evaluation.'; return; }
    const data = await response.json(); window.location.href = `/evaluations/${data.id}/view`;
  });

  const workflow = document.querySelector('.workflow-actions');
  if (workflow) {
    const message = document.getElementById('workflow-message');
    const dialog = document.getElementById('return-dialog');
    const performWorkflow = async (action, comment = null) => {
      const button = workflow.querySelector(`[data-action="${action}"]`);
      if (button) button.disabled = true;
      const response = await fetch(`/api/evaluations/${workflow.dataset.id}/workflow/${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(comment === null ? {} : {comment})
      });
      if (response.ok) location.reload();
      else {
        if (button) button.disabled = false;
        const data = await response.json();
        message.className = 'error';
        message.textContent = data.detail || 'Unable to update the workflow.';
      }
    };
    workflow.querySelectorAll('[data-action]').forEach(button => {
      button.addEventListener('click', () => {
        const action = button.dataset.action;
        if (action === 'return' && dialog) dialog.showModal();
        else performWorkflow(action);
      });
    });
    const returnForm = document.getElementById('return-form');
    if (returnForm) returnForm.addEventListener('submit', event => {
      event.preventDefault();
      if (!returnForm.reportValidity()) return;
      const comment = document.getElementById('return-comment').value.trim();
      dialog.close();
      performWorkflow('return', comment);
    });
    document.querySelector('[data-close-return]')?.addEventListener('click', () => dialog.close());
  }
  const userForm=document.getElementById('user-form');
  if(userForm){userForm.addEventListener('submit',async event=>{event.preventDefault(); const fd=new FormData(userForm); const payload=Object.fromEntries(fd.entries()); const message=document.getElementById('user-message'); const response=await fetch('/api/admin/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const data=await response.json(); if(!response.ok){message.className='error'; message.textContent=typeof data.detail==='string'?data.detail:'Unable to create user.'; return;} message.className=''; message.textContent='User created. Provide the temporary password through an approved secure channel.'; location.reload();});}
  document.querySelectorAll('.user-status').forEach(button=>button.addEventListener('click',async()=>{const active=button.dataset.active==='true'; if(!confirm(`${active?'Reactivate':'Deactivate'} this user?`))return; const response=await fetch(`/api/admin/users/${button.dataset.userId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active})}); if(response.ok)location.reload(); else alert((await response.json()).detail);}));
})();
