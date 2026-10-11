"""Bounded JSON-LD summaries evaluated inside the observed document (WS-40).

Only supported fields leave Chromium. No HTML transfer, remote contexts, fetching,
or JSON-LD execution; malformed scripts are ignored independently. Limits bound
parsing/traversal and output even for hostile or unusually large documents.
"""

from __future__ import annotations

from playwright.async_api import Frame, Page


MAX_PAGE_DATA_CHARS = 1200
MAX_PAGE_DATA_ITEMS = 5

_SUMMARY_JS = r"""
() => {
  const supported = new Set(['Product', 'Offer', 'AggregateOffer', 'Flight',
                             'Event', 'Article', 'NewsArticle', 'BreadcrumbList']);
  const clean = (value, cap = 128) => {
    if (typeof value !== 'string' && typeof value !== 'number') return '';
    if (typeof value === 'number' && !Number.isFinite(value)) return '';
    // Inspect a bounded prefix before replacing/normalizing a potentially 1MB value.
    const prefix = String(value).slice(0, 2048).replace(/[\uD800-\uDBFF]$/, '')
      .replace(/[\x00-\x1f\x7f-\x9f]/g, '').replace(/\s+/g, ' ').trim();
    // Cap Unicode characters rather than UTF-16 units (avoid half an emoji).
    return Array.from(prefix).slice(0, cap).join('');
  };
  const named = value => clean(value && typeof value === 'object' ? value.name : value);
  const localURL = value => {
    if (typeof value !== 'string' || value.length > 256) return '';
    try {
      const url = new URL(value, location.href);
      if (!['http:', 'https:'].includes(url.protocol) || url.origin !== location.origin
          || url.username || url.password || url.href.length > 256) return '';
      return url.href;
    } catch (_) { return ''; }
  };
  const fields = (obj, keys, cap = 64) => {
    const out = {};
    for (const key of keys) {
      const value = clean(obj[key], cap);
      if (value) out[key] = value;
    }
    return out;
  };
  const offer = obj => {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
    const out = fields(obj, ['price', 'priceCurrency', 'availability',
                             'lowPrice', 'highPrice', 'offerCount']);
    const type = Array.isArray(obj['@type']) ? obj['@type'].find(t =>
      t === 'Offer' || t === 'AggregateOffer') : obj['@type'];
    if (type === 'Offer' || type === 'AggregateOffer') out['@type'] = type;
    const url = localURL(obj.url);
    if (url) out.url = url;
    return Object.keys(out).some(k => k !== '@type') ? out : null;
  };
  const summarize = (obj, type) => {
    let out = {'@type': type};
    if (type === 'Offer' || type === 'AggregateOffer') {
      const details = offer(obj);
      if (details) Object.assign(out, details);
    }
    const name = clean(obj.name || obj.headline || (type === 'Flight' ? obj.flightNumber : ''));
    if (name) out.name = name;
    if (type === 'BreadcrumbList') {
      const entries = Array.isArray(obj.itemListElement) ? obj.itemListElement : [];
      const names = entries.slice(0, 5).map(x => x && named(x.name || x.item)).filter(Boolean);
      if (names.length) out.names = names;
    } else {
      Object.assign(out, fields(obj, ['sku']));
      const brand = named(obj.brand);
      if (brand) out.brand = brand;
      const offers = (Array.isArray(obj.offers) ? obj.offers : [obj.offers])
        .slice(0, 2).map(offer).filter(Boolean);
      if (offers.length) out.offers = offers;
      if (obj.aggregateRating && typeof obj.aggregateRating === 'object') {
        const rating = fields(obj.aggregateRating, ['ratingValue', 'ratingCount',
                                                   'reviewCount', 'bestRating', 'worstRating']);
        if (Object.keys(rating).length) out.aggregateRating = rating;
      }
    }
    return Object.keys(out).length > 1 ? out : null;
  };
  const result = [];
  let visited = 0, inputChars = 0;
  const add = item => {
    // Keep item/field boundaries; never return sliced or malformed JSON.
    while (JSON.stringify([...result, item]).length > 1200) {
      const keys = Object.keys(item);
      if (keys.length <= 2) return;
      delete item[keys[keys.length - 1]];
    }
    result.push(item);
  };
  const walk = (node, depth = 0) => {
    if (!node || typeof node !== 'object' || depth > 24 || ++visited > 5000
        || result.length >= 5) return;
    if (Array.isArray(node)) {
      for (const entry of node) {
        if (visited > 5000 || result.length >= 5) break;
        walk(entry, depth + 1);
      }
      return;
    }
    const types = Array.isArray(node['@type']) ? node['@type'] : [node['@type']];
    const type = types.find(t => supported.has(t));
    if (type) {
      const item = summarize(node, type);
      if (item) add(item);
    }
    for (const key of Object.keys(node)) {
      if (visited > 5000 || result.length >= 5) break;
      // Nested offers already summarized on their parent; don't duplicate them.
      if (key !== 'offers' || !type) walk(node[key], depth + 1);
    }
  };
  const scripts = document.querySelectorAll('script[type="application/ld+json" i]');
  for (let i = 0; i < Math.min(scripts.length, 32); i++) {
    if (result.length >= 5) break;
    const text = scripts[i].textContent || '';
    inputChars += text.length;
    if (text.length > 1100000 || inputChars > 2200000) continue;
    try { walk(JSON.parse(text)); } catch (_) { /* ignore malformed scripts */ }
  }
  return result;
}
"""


async def summarize_page_data(page: Page | Frame) -> list[dict[str, object]]:
    """Read at most five useful entries, with a compact JSON budget of 1,200 chars.

    A navigation race/closed page must not turn an otherwise successful observation
    into a failure. Cancellation still propagates.
    """
    try:
        result = await page.evaluate(_SUMMARY_JS)
    except Exception:  # noqa: BLE001 - page may navigate during observation
        return []
    return result if isinstance(result, list) else []
