(() => {
  const menuRoot = document.querySelector('[data-order-page]');
  if (!menuRoot) return;

  const tableSlug = menuRoot.dataset.tableSlug;
  const items = JSON.parse(document.getElementById('menu-data').textContent);
  const cartKey = `qr-order-cart:${tableSlug}`;
  const cartList = document.querySelector('[data-cart-list]');
  const cartTotal = document.querySelector('[data-cart-total]');
  const cartCount = document.querySelector('[data-cart-count]');
  const form = document.querySelector('[data-checkout-form]');
  const submitButton = document.querySelector('[data-submit-order]');
  const emptyState = document.querySelector('[data-cart-empty]');

  const currency = new Intl.NumberFormat('zh-TW', {
    style: 'currency',
    currency: 'TWD',
    maximumFractionDigits: 0
  });

  function loadCart() {
    try {
      return JSON.parse(localStorage.getItem(cartKey)) || {};
    } catch {
      return {};
    }
  }

  function saveCart(cart) {
    localStorage.setItem(cartKey, JSON.stringify(cart));
  }

  function findItem(id) {
    return items.find((item) => Number(item.id) === Number(id));
  }

  function renderCart() {
    const cart = loadCart();
    const entries = Object.entries(cart)
      .map(([id, quantity]) => {
        const item = findItem(id);
        return item ? { ...item, quantity } : null;
      })
      .filter(Boolean);

    cartList.innerHTML = '';

    if (!entries.length) {
      emptyState.hidden = false;
      cartTotal.textContent = currency.format(0);
      cartCount.textContent = '0';
      submitButton.disabled = true;
      return;
    }

    emptyState.hidden = true;
    submitButton.disabled = false;

    let total = 0;
    let count = 0;

    for (const entry of entries) {
      const subtotal = entry.price * entry.quantity;
      total += subtotal;
      count += entry.quantity;

      const row = document.createElement('div');
      row.className = 'cart-row';
      row.innerHTML = `
        <div>
          <strong>${entry.name}</strong>
          <div class="muted">${currency.format(entry.price)} × ${entry.quantity}</div>
        </div>
        <div>
          <div class="cart-actions">
            <button type="button" class="qty-control-button" data-action="minus" data-id="${entry.id}">−</button>
            <button type="button" class="qty-control-button" data-action="plus" data-id="${entry.id}">+</button>
            <button type="button" class="qty-control-button" data-action="remove" data-id="${entry.id}">刪除</button>
          </div>
          <div style="text-align:right;margin-top:8px;font-weight:700">${currency.format(subtotal)}</div>
        </div>
      `;
      cartList.appendChild(row);
    }

    cartTotal.textContent = currency.format(total);
    cartCount.textContent = String(count);
  }

  function updateCart(id, delta) {
    const cart = loadCart();
    cart[id] = (cart[id] || 0) + delta;
    if (cart[id] <= 0) delete cart[id];
    saveCart(cart);
    renderCart();
  }

  function removeItem(id) {
    const cart = loadCart();
    delete cart[id];
    saveCart(cart);
    renderCart();
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-add-to-cart], [data-action]');
    if (!button) return;

    const id = button.dataset.id;
    if (button.matches('[data-add-to-cart]')) {
      updateCart(id, 1);
      return;
    }

    const action = button.dataset.action;
    if (action === 'plus') updateCart(id, 1);
    if (action === 'minus') updateCart(id, -1);
    if (action === 'remove') removeItem(id);
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const cart = loadCart();
    const payloadItems = Object.entries(cart).map(([menu_item_id, quantity]) => ({
      menu_item_id: Number(menu_item_id),
      quantity: Number(quantity)
    }));

    if (!payloadItems.length) {
      alert('購物車是空的，請先加入商品。');
      return;
    }

    submitButton.disabled = true;
    submitButton.textContent = '送出中...';

    try {
      const response = await fetch('/api/orders', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          table_slug: tableSlug,
          customer_name: form.customer_name.value,
          note: form.note.value,
          items: payloadItems
        })
      });

      const result = await response.json();
      if (!result.ok) {
        throw new Error(result.message || '下單失敗');
      }

      localStorage.removeItem(cartKey);
      window.location.href = result.redirect;
    } catch (error) {
      alert(error.message);
      submitButton.disabled = false;
      submitButton.textContent = '送出訂單';
    }
  });

  renderCart();
})();
