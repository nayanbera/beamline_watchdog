/* Click-to-sort for every table.table that has a single header row.
   Click a header: ascending, again: descending. Rows with empty values ("—") always sort last.
   Numbers (incl. memory units and "Up 2h 22m" durations) sort numerically, other text naturally.
   Opt out with data-nosort on the table or on a <th>; a cell's data-sort overrides its text.
   Tables that update live (dashboard) are re-sorted after each refresh. */
(function () {
  const SKIP = /^(actions?|control|monitor|edit)?$/i;
  const UNIT = {K: 1024, M: 1048576, G: 1073741824, T: 1099511627776};

  function raw(cell) {
    if (!cell) return '';
    return (cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent).replace(/\s+/g, ' ').trim();
  }
  function key(text) {
    if (text === '' || text === '—' || text === '-') return null;
    let m = text.match(/^(?:Up|Down)?\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$/i);
    if (m && (m[1] || m[2] || m[3] || m[4]))
      return {n: (+m[1] || 0) * 86400 + (+m[2] || 0) * 3600 + (+m[3] || 0) * 60 + (+m[4] || 0)};
    m = text.match(/^([-+]?\d*\.?\d+)\s*([KMGT])B\b/i);
    if (m) return {n: parseFloat(m[1]) * UNIT[m[2].toUpperCase()]};
    m = text.match(/^([-+]?\d*\.?\d+(?:e[-+]?\d+)?)(?=\s|$|%)/i);
    if (m) return {n: parseFloat(m[1])};
    return {s: text.toLowerCase()};
  }
  function compare(a, b) {
    if (a.n !== undefined && b.n !== undefined) return a.n - b.n;
    if (a.n !== undefined) return -1;           // numbers before text
    if (b.n !== undefined) return 1;
    return a.s.localeCompare(b.s, undefined, {numeric: true});
  }

  // The cell covering column `col`, counting colspans (rows may merge columns, e.g. "no data" cells).
  function cellAt(row, col) {
    let c = 0;
    for (const cell of row.cells) {
      c += cell.colSpan || 1;
      if (col < c) return cell;
    }
    return null;
  }
  // A data row spans exactly the header's columns; "No items" placeholders span them with one cell.
  function isDataRow(row, ncols) {
    let c = 0;
    for (const cell of row.cells) c += cell.colSpan || 1;
    return c === ncols && !(row.cells.length === 1 && ncols > 1);
  }

  document.querySelectorAll('table.table').forEach((table, ti) => {
    const heads = table.tHead && table.tHead.rows.length === 1 ? [...table.tHead.rows[0].cells] : null;
    const body = table.tBodies[0];
    if (!heads || !body || table.hasAttribute('data-nosort')) return;
    const store = 'sort:' + location.pathname + ':' + ti;
    let state = null;                            // {col, dir}
    try { state = JSON.parse(sessionStorage.getItem(store)); } catch (e) {}

    function paint() {
      heads.forEach((th, i) => {
        const ind = th.querySelector('.sort-ind');
        if (ind) ind.textContent = state && state.col === i ? (state.dir > 0 ? ' ▲' : ' ▼') : '';
      });
    }
    function apply() {
      if (!state) return;
      observer.disconnect();
      const rows = [...body.rows].filter(r => isDataRow(r, heads.length));
      const keyed = rows.map(r => ({r, k: key(raw(cellAt(r, state.col)))}));
      keyed.sort((x, y) => {
        if (!x.k || !y.k) return !x.k && !y.k ? 0 : (!x.k ? 1 : -1);   // empties last
        return state.dir * compare(x.k, y.k);
      });
      keyed.forEach(o => body.appendChild(o.r));
      paint();
      observer.observe(body, watch);
    }
    let timer = null;
    const watch = {childList: true, subtree: true, characterData: true};
    const observer = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(apply, 250); });

    heads.forEach((th, i) => {
      if (th.hasAttribute('data-nosort') || th.querySelector('input') || SKIP.test(th.textContent.trim())) return;
      th.style.cursor = 'pointer';
      th.style.userSelect = 'none';
      th.title = (th.title ? th.title + ' — ' : '') + 'Click to sort';
      th.insertAdjacentHTML('beforeend', '<span class="sort-ind"></span>');
      th.addEventListener('click', () => {
        state = {col: i, dir: state && state.col === i ? -state.dir : 1};
        try { sessionStorage.setItem(store, JSON.stringify(state)); } catch (e) {}
        apply();
      });
    });
    if (state && (state.col >= heads.length || !heads[state.col].querySelector('.sort-ind'))) state = null;
    observer.observe(body, watch);
    apply();
  });
})();
