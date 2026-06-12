const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const SOURCE_LABELS = {
  redis: "⚡ Redis cache",
  pinecone: "📦 Pinecone vector DB",
  live_search: "🌐 Live web search",
};

const form = $("#lookup-form");
const barcodeInput = $("#barcode-input");
const forceRefreshInput = $("#force-refresh");
const lookupBtn = $("#lookup-btn");
const btnLabel = lookupBtn.querySelector(".btn__label");
const btnSpinner = lookupBtn.querySelector(".btn__spinner");

const alertEl = $("#alert");
const loadingEl = $("#loading");
const resultsEl = $("#results");
const apiStatus = $("#api-status");

function showAlert(message, type = "error") {
  alertEl.textContent = message;
  alertEl.className = `alert alert--${type}`;
  alertEl.hidden = false;
}

function hideAlert() {
  alertEl.hidden = true;
}

function setLoading(active) {
  loadingEl.hidden = !active;
  lookupBtn.disabled = active;
  btnLabel.hidden = active;
  btnSpinner.hidden = !active;
  if (active) {
    resultsEl.hidden = true;
    hideAlert();
  }
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function stockIcon(inStock) {
  if (inStock === true) return '<span class="stock--yes" aria-label="In stock">✓</span>';
  if (inStock === false) return '<span class="stock--no" aria-label="Out of stock">✗</span>';
  return '<span class="stock--unknown" aria-label="Stock unknown">?</span>';
}

function renderVerifiedBadge(data) {
  const badge = $("#verified-badge");
  if (data.barcode_verified) {
    badge.textContent = `Verified (${data.identity_confidence})`;
    badge.className = `verified-badge verified-badge--${data.identity_confidence === "high" ? "ok" : "warn"}`;
  } else {
    badge.textContent = `Not verified (${data.identity_confidence})`;
    badge.className = "verified-badge verified-badge--bad";
  }
}

function renderSourceBadge(source) {
  const badge = $("#source-badge");
  badge.textContent = SOURCE_LABELS[source] || "Result";
}

function renderProductHero(data) {
  const img = $("#product-image");
  const fallback = $("#product-image-fallback");

  if (data.image_url) {
    img.src = data.image_url;
    img.alt = data.product_name || "Product image";
    img.hidden = false;
    fallback.hidden = true;
    img.onerror = () => {
      img.hidden = true;
      fallback.hidden = false;
    };
  } else {
    img.hidden = true;
    fallback.hidden = false;
  }

  $("#product-brand").textContent = data.brand || "—";
  $("#product-name").textContent = data.product_name || "Unknown Product";
  $("#product-barcode").textContent = data.barcode || "";

  const tags = $("#product-tags");
  tags.innerHTML = "";
  if (data.target_animal) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = data.target_animal;
    tags.appendChild(tag);
  }
  if (data.identity_confidence) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = `${data.identity_confidence} confidence`;
    tags.appendChild(tag);
  }

  const links = $("#product-links");
  links.innerHTML = "";
  if (data.manufacturer_url) {
    links.innerHTML += `<a class="link-btn" href="${escapeHtml(data.manufacturer_url)}" target="_blank" rel="noopener">Manufacturer ↗</a>`;
  }
  if (data.source_urls?.length) {
    links.innerHTML += `<a class="link-btn" href="${escapeHtml(data.source_urls[0])}" target="_blank" rel="noopener">Evidence ↗</a>`;
  }

  const bestPrice = $("#best-price");
  if (data.best_price) {
    bestPrice.hidden = false;
    $("#best-price-value").textContent = data.best_price.price_display || `HK$${data.best_price.price}`;
    $("#best-price-store").textContent = data.best_price.retailer_name || data.best_price.store || "";
  } else {
    bestPrice.hidden = true;
  }
}

function renderProductDetails(data) {
  const dl = $("#product-details");
  const rows = [
    ["Barcode", `<code>${escapeHtml(data.barcode)}</code>`],
    ["Barcode Match", data.barcode_verified
      ? `Verified (${escapeHtml(data.identity_confidence)})`
      : `Not verified (${escapeHtml(data.identity_confidence)})`],
    ["Product Name", escapeHtml(data.product_name)],
    ["Brand", escapeHtml(data.brand) || "—"],
    ["Target Animal", escapeHtml(data.target_animal) || "—"],
  ];

  if (data.barcode_evidence) {
    const evidence = data.barcode_evidence.length > 200
      ? `${escapeHtml(data.barcode_evidence.slice(0, 200))}…`
      : escapeHtml(data.barcode_evidence);
    rows.push(["Evidence", evidence]);
  }
  if (data.source_urls?.length) {
    rows.push(["Evidence URL", `<a href="${escapeHtml(data.source_urls[0])}" target="_blank" rel="noopener">${escapeHtml(data.source_urls[0])}</a>`]);
  }
  if (data.manufacturer_url) {
    rows.push(["Manufacturer URL", `<a href="${escapeHtml(data.manufacturer_url)}" target="_blank" rel="noopener">${escapeHtml(data.manufacturer_url)}</a>`]);
  }
  if (data.image_url) {
    rows.push(["Image URL", `<a href="${escapeHtml(data.image_url)}" target="_blank" rel="noopener">${escapeHtml(data.image_url)}</a>`]);
  }

  dl.innerHTML = rows
    .map(([label, value]) => `
      <div class="detail-list__row">
        <dt>${escapeHtml(label)}</dt>
        <dd>${value}</dd>
      </div>
    `)
    .join("");
}

function renderNutrition(data) {
  const container = $("#nutrition-content");
  const items = data.nutrition_display || [];

  if (!items.length) {
    container.innerHTML = '<p class="empty-state">No nutritional data available for this product.</p>';
    return;
  }

  container.innerHTML = items
    .map((item) => `
      <div class="nutrition-item">
        <p class="nutrition-item__label">${escapeHtml(item.label)}</p>
        <p class="nutrition-item__value">${escapeHtml(item.value)}</p>
      </div>
    `)
    .join("");
}

function renderRetailers(data) {
  const prices = data.price_comparison || [];
  const tbody = $("#retailer-tbody");
  const cards = $("#retailer-cards");
  const noRetailers = $("#no-retailers");

  if (!prices.length) {
    tbody.innerHTML = "";
    cards.innerHTML = "";
    cards.hidden = true;
    noRetailers.hidden = false;
    return;
  }

  noRetailers.hidden = true;

  tbody.innerHTML = prices
    .map((r, i) => `
      <tr>
        <td>${i + 1}</td>
        <td><strong>${escapeHtml(r.retailer_name)}</strong></td>
        <td class="price">${escapeHtml(r.price_display || `HK$${r.price}`)}</td>
        <td>${stockIcon(r.in_stock)}</td>
        <td><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.url)}</a></td>
      </tr>
    `)
    .join("");

  cards.innerHTML = prices
    .map((r, i) => `
      <article class="retailer-card">
        <div class="retailer-card__head">
          <span class="retailer-card__name">#${i + 1} ${escapeHtml(r.retailer_name)}</span>
          <span class="retailer-card__price">${escapeHtml(r.price_display || `HK$${r.price}`)}</span>
        </div>
        <p class="retailer-card__meta">Stock: ${r.in_stock === true ? "In stock" : r.in_stock === false ? "Out of stock" : "Unknown"}</p>
        <a class="retailer-card__link" href="${escapeHtml(r.url)}" target="_blank" rel="noopener">View product ↗</a>
      </article>
    `)
    .join("");
}

function renderWarnings(data) {
  const el = $("#warnings");
  if (!data.warnings?.length) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.innerHTML = `<strong>Good to know</strong><ul>${data.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
}

function renderResults(data) {
  renderSourceBadge(data.source);
  renderVerifiedBadge(data);
  renderProductHero(data);
  renderWarnings(data);
  renderProductDetails(data);
  renderNutrition(data);
  renderRetailers(data);
  resultsEl.hidden = false;
}

async function checkHealth() {
  try {
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error("unhealthy");
    apiStatus.textContent = "API online";
    apiStatus.dataset.state = "ok";
  } catch {
    apiStatus.textContent = "API offline";
    apiStatus.dataset.state = "error";
  }
}

async function lookup(barcode, forceRefresh = false) {
  const params = new URLSearchParams({ barcode, force_refresh: String(forceRefresh) });
  const res = await fetch(`/api/lookup?${params}`);
  const body = await res.json().catch(() => ({}));

  if (!res.ok) {
    const detail = body.detail || body.error || `Request failed (${res.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  if (!body.success) {
    if (body.data) {
      renderResults(body.data);
      showAlert(body.error || "Product could not be verified.", "warning");
      return;
    }
    throw new Error(body.error || "Product not found");
  }

  renderResults(body.data);
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const barcode = barcodeInput.value.trim();
  if (!barcode) {
    showAlert("Please enter a barcode.");
    barcodeInput.focus();
    return;
  }

  setLoading(true);
  try {
    await lookup(barcode, forceRefreshInput.checked);
    hideAlert();
  } catch (err) {
    showAlert(err.message || "Lookup failed. Please try again.");
  } finally {
    setLoading(false);
  }
});

$$(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    barcodeInput.value = chip.dataset.barcode;
    barcodeInput.focus();
    form.requestSubmit();
  });
});

const urlParams = new URLSearchParams(window.location.search);
const initialBarcode = urlParams.get("barcode");
if (initialBarcode) {
  barcodeInput.value = initialBarcode;
  form.requestSubmit();
}

checkHealth();
