let cart = [];

function fmtPrice(usd) {
  const cur = window.IPNET_CURRENCY || { symbol: "$", rate: 1 };
  const converted = usd * cur.rate;
  const formatted = converted >= 1000 ? converted.toLocaleString(undefined, { maximumFractionDigits: 0 }) : converted.toFixed(2);
  return cur.symbol + formatted;
}

function saveCart() {
  document.getElementById("cart-count").textContent = cart.reduce((s, i) => s + i.qty, 0);
}

function renderCart() {
  const container = document.getElementById("cart-items");
  container.innerHTML = "";
  let total = 0;
  cart.forEach((item, idx) => {
    total += item.price * item.qty;
    const div = document.createElement("div");
    div.className = "cart-item";
    div.innerHTML = `<span>${item.title} x${item.qty}</span><span>${fmtPrice(item.price * item.qty)} <button data-idx="${idx}" class="remove-item">✕</button></span>`;
    container.appendChild(div);
  });
  document.getElementById("cart-total").textContent = fmtPrice(total);
  document.querySelectorAll(".remove-item").forEach(btn => {
    btn.addEventListener("click", (e) => {
      cart.splice(parseInt(e.target.dataset.idx), 1);
      renderCart();
      saveCart();
    });
  });
}

document.addEventListener("click", (e) => {
  if (e.target.classList.contains("add-to-cart")) {
    const id = parseInt(e.target.dataset.id);
    const title = e.target.dataset.title;
    const price = parseFloat(e.target.dataset.price);
    const existing = cart.find(i => i.id === id);
    if (existing) existing.qty += 1;
    else cart.push({ id, title, price, qty: 1 });
    renderCart();
    saveCart();
    document.getElementById("cart-drawer").classList.add("open");
  }
});

document.getElementById("cart-btn").addEventListener("click", () => {
  document.getElementById("cart-drawer").classList.toggle("open");
});
document.getElementById("cart-close").addEventListener("click", () => {
  document.getElementById("cart-drawer").classList.remove("open");
});

document.getElementById("cart-checkout-btn").addEventListener("click", () => {
  if (cart.length === 0) { alert("Your cart is empty."); return; }
  document.getElementById("checkout-modal").classList.add("open");
});
document.getElementById("chk-cancel").addEventListener("click", () => {
  document.getElementById("checkout-modal").classList.remove("open");
});

document.getElementById("chk-submit").addEventListener("click", async () => {
  const name = document.getElementById("chk-name").value.trim();
  const contact = document.getElementById("chk-contact").value.trim();
  const country = document.getElementById("chk-country").value.trim();
  const isLoggedIn = document.body.dataset.loggedIn === "1";
  if (!isLoggedIn && (!name || !contact)) {
    document.getElementById("chk-result").innerHTML = '<p style="color:#ef4444">Please enter your name and a contact method.</p>';
    return;
  }
  const resp = await fetch("/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: cart, name, contact, country })
  });
  const data = await resp.json();
  if (data.order_ref) {
    const msg = data.paid_from_wallet
      ? `Paid from wallet! Order reference: <strong>${data.order_ref}</strong>. Check your dashboard for delivery.`
      : `Order placed! Reference: <strong>${data.order_ref}</strong>`;
    document.getElementById("chk-result").innerHTML = `<p style="color:#22d3a5">${msg}</p>`;
    cart = [];
    renderCart();
    saveCart();
    setTimeout(() => {
      window.location.href = data.paid_from_wallet ? "/dashboard" : "/order/" + data.order_ref;
    }, 1400);
  } else if (data.insufficient_balance) {
    document.getElementById("chk-result").innerHTML =
      `<p style="color:#ef4444">${data.error}</p><a href="/dashboard" style="color:#5b7fff;">Fund your wallet →</a>`;
  } else {
    document.getElementById("chk-result").innerHTML = `<p style="color:#ef4444">${data.error || "Something went wrong."}</p>`;
  }
});
