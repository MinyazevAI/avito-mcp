// Прототип разбора панели фильтров Авито: группы по заголовкам <h5> и элементы с data-marker.
// Вызывается как page.evaluate(code, rootSelector). Возвращает компактный список фильтров.
(rootSel) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('[data-marker="search-filters"]') || document;
  const clean = (s) => (s || '').replace(/[\s ]+/g, ' ').trim();
  const txt = (e) => e ? clean(e.innerText || e.textContent) : '';
  const dm = (e) => e.getAttribute('data-marker') || '';
  const keyOf = (m) => { const r = m.match(/^(params\[\d+\]|price|[A-Za-z]+(?:\/[A-Za-z]+)?)/); return r ? r[1] : m; };

  // 1. Все «ключевые» элементы управления
  const ctrls = [];
  for (const el of root.querySelectorAll('[data-marker]')) {
    const m = dm(el);
    let kind = null, value = null;
    let r;
    if ((r = m.match(/^(.+)\/checkbox\/([^/]+)$/)) && r[2] !== 'toggle') {
      kind = 'checkbox'; value = r[2];
    } else if ((r = m.match(/^(.+)\/option\(([^)]*)\)$/)) && el.tagName === 'LABEL') {
      kind = 'radio'; value = r[2];
    } else if (/\/select$/.test(m) && el.tagName === 'SELECT') {
      kind = 'select';
    } else if ((r = m.match(/^(.+)-(from|to)\/input$/))) {
      kind = 'range_' + r[2];
    } else if (/\/input$/.test(m) && el.tagName === 'INPUT') {
      kind = 'text';
    } else if (m === 'geoFilters/select') {
      kind = 'geo';
    } else if (/\/show-button$/.test(m)) {
      kind = 'more';
    } else if (/^\w+\/\w+\/\w+$/.test(m) && el.tagName === 'LABEL') {
      kind = 'checkbox'; value = m.split('/').pop();      // filters/withImagesOnly/withImagesOnly
    }
    if (kind) ctrls.push({el, m, kind, value});
  }

  // 2. Заголовок группы: ближайший предыдущий h5 в пределах общего предка
  // Идём вверх до первого предка, где есть заголовок; если он там один — это заголовок группы.
  const titleFor = (el) => {
    let p = el.parentElement;
    while (p && p !== root.parentElement) {
      const hs = p.querySelectorAll('h5, h4, h3');
      if (hs.length === 1) return txt(hs[0]);
      if (hs.length > 1) return null;
      p = p.parentElement;
    }
    return null;
  };

  // 3. Группировка по ключу (params[ID], price, footWalkingMetro, …)
  const groups = new Map();
  for (const c of ctrls) {
    const key = c.kind === 'geo' ? 'geo' : keyOf(c.m.replace(/-(from|to)\/input$/, '').replace(/\/(checkbox|option|select|input|show-button).*$/, ''));
    if (!groups.has(key)) groups.set(key, {key, title: null, kinds: new Set(), options: [], from: null, to: null, more: false, selected: []});
    const g = groups.get(key);
    if (!g.title) g.title = titleFor(c.el);
    g.kinds.add(c.kind);
    if (c.kind === 'checkbox') {
      const on = c.el.getAttribute('aria-checked') === 'true' || !!(c.el.querySelector('input') || {}).checked;
      g.options.push({value: c.value, label: txt(c.el)});
      if (on) g.selected.push(txt(c.el));
    } else if (c.kind === 'radio') {
      const inp = c.el.querySelector('input');
      const on = (inp && inp.checked) || c.el.getAttribute('aria-checked') === 'true' || c.el.getAttribute('aria-pressed') === 'true';
      g.options.push({value: c.value, label: txt(c.el)});
      if (on) g.selected.push(txt(c.el));
    } else if (c.kind === 'select') {
      for (const o of c.el.options) g.options.push({value: o.value, label: clean(o.textContent)});
      const so = c.el.options[c.el.selectedIndex];
      if (so) g.selected.push(clean(so.textContent));
    } else if (c.kind === 'range_from') {
      g.from = c.el.value || null; g.placeholder_from = c.el.getAttribute('placeholder');
    } else if (c.kind === 'range_to') {
      g.to = c.el.value || null; g.placeholder_to = c.el.getAttribute('placeholder');
    } else if (c.kind === 'text') {
      g.text = c.el.value || null; g.placeholder = c.el.getAttribute('placeholder');
    } else if (c.kind === 'geo') {
      g.text = txt(c.el);
    } else if (c.kind === 'more') {
      g.more = true;
    }
  }

  const out = [];
  for (const g of groups.values()) {
    const k = Array.from(g.kinds).filter(x => x !== 'more');
    let type = k.includes('checkbox') ? 'checkboxes' : k.includes('radio') ? 'switch' : k.includes('select') ? 'list'
             : (k.includes('range_from') || k.includes('range_to')) ? 'range' : k.includes('geo') ? 'location' : 'text';
    let title = g.title;
    if (type === 'checkboxes' && g.options.length === 1 && !title) title = g.options[0].label;
    out.push(Object.fromEntries(Object.entries({
      name: title, key: g.key, type,
      options: g.options.length ? g.options.map(o => o.label) : undefined,
      values: g.options.length ? g.options.map(o => o.value) : undefined,
      selected: g.selected.length ? g.selected : undefined,
      from: g.from || undefined, to: g.to || undefined, text: g.text || undefined,
      placeholder: g.placeholder || undefined,
      has_more: g.more || undefined,
    }).filter(([, v]) => v !== undefined)));
  }
  return out;
}
