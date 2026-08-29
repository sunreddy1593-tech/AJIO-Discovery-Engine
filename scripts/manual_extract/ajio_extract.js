/**
 * Extract visible AJIO Q&A and reviews into the Collect JSON shape.
 *
 * This file is an IIFE. Bookmarklet and Playwright both evaluate it as-is and
 * receive `{ documents, warnings }`. Scroll the Ratings & Reviews / Q&A section
 * into view first — the snippet reads the DOM that is already on the page, it
 * does not paginate the catalog.
 *
 * Bookmarklet: prefix the whole file with `javascript:` and wrap the return
 * value with a clipboard copy (see README.md in this folder).
 *
 * Akamai blocks automated headless clients, not a person reading the page.
 * Do not call this from a spawned Chromium; attach to a real session instead.
 */
(function extractAjioVisible() {
  var VIA =
    (typeof window !== "undefined" && window.__AJIO_EXTRACT_VIA__) ||
    "bookmarklet";
  var documents = [];
  var warnings = [];
  var seen = Object.create(null);

  function textOf(node) {
    if (!node) return "";
    var clone = node.cloneNode(true);
    clone.querySelectorAll("button, svg, script, style, noscript").forEach(function (el) {
      el.remove();
    });
    return (clone.innerText || clone.textContent || "").replace(/\s+/g, " ").trim();
  }

  function firstMatch(selectors, root) {
    root = root || document;
    for (var i = 0; i < selectors.length; i++) {
      var found = root.querySelectorAll(selectors[i]);
      if (found && found.length) return Array.prototype.slice.call(found);
    }
    return [];
  }

  function productIdFrom(href) {
    var match = String(href || "").match(/\/p\/(\d{6,})/);
    return match ? match[1] : null;
  }

  function productId() {
    return (
      productIdFrom(location.href) ||
      productIdFrom(document.querySelector("link[rel='canonical']") &&
        document.querySelector("link[rel='canonical']").href) ||
      (function () {
        var link = document.querySelector("a[href*='/p/']");
        return link ? productIdFrom(link.href) : null;
      })()
    );
  }

  function productTitle() {
    var h1 = document.querySelector("h1, h1 span, [class*='prod-name'], [class*='product-title']");
    return h1 ? textOf(h1) : null;
  }

  function ratingFrom(node) {
    var labeled = node.getAttribute && node.getAttribute("aria-label");
    var match = String(labeled || textOf(node) || "").match(/([1-5](?:\.\d)?)\s*(?:\/\s*5|star)/i);
    if (match) return Number(match[1]);
    var star = node.querySelector && node.querySelector("[itemprop='ratingValue'], [class*='rating']");
    if (star) {
      var n = parseFloat(star.getAttribute("content") || textOf(star));
      if (!isNaN(n) && n >= 1 && n <= 5) return n;
    }
    return null;
  }

  function pushDoc(doc) {
    var key = (doc.meta.content_type || "") + "|" + (doc.text || "").toLowerCase();
    if (!doc.text || seen[key]) return;
    seen[key] = true;
    documents.push(doc);
  }

  var pid = productId();
  var title = productTitle();
  var url = pid ? "https://www.ajio.com/p/" + pid : location.href.split("?")[0];
  if (!pid) {
    warnings.push("no /p/<id> in the URL; meta.product_id will fail validation until you add it");
  }

  function baseMeta(contentType, extra) {
    var meta = {
      content_type: contentType,
      product_id: pid,
      product_title: title,
      extraction: VIA,
    };
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        if (extra[k] != null) meta[k] = extra[k];
      });
    }
    return meta;
  }

  // JSON-LD first: stable across CSS renames, carries rating and date as data.
  document.querySelectorAll('script[type="application/ld+json"]').forEach(function (node) {
    var payload;
    try {
      payload = JSON.parse(node.textContent);
    } catch (err) {
      return;
    }
    var blocks = Array.isArray(payload) ? payload : [payload];
    blocks.forEach(function (block) {
      if (!block || typeof block !== "object") return;
      var reviews = block.review || [];
      if (!Array.isArray(reviews)) reviews = [reviews];
      reviews.forEach(function (review) {
        if (!review || typeof review !== "object") return;
        var body = review.reviewBody || review.description;
        if (!body) return;
        var author = review.author;
        pushDoc({
          source: "ajio_manual",
          url: url,
          text: String(body).replace(/\s+/g, " ").trim(),
          author: author && typeof author === "object" ? author.name || null : author || null,
          timestamp: review.datePublished || null,
          meta: baseMeta("review", {
            rating: review.reviewRating && review.reviewRating.ratingValue,
            review_title: review.name || null,
            brand: block.brand && (block.brand.name || block.brand),
          }),
        });
      });
    });
  });

  var reviewNodes = firstMatch([
    "div.review-item",
    "div.user-review",
    "li.review",
    "[itemprop='review']",
    "div[class*='review-list'] div[class*='item']",
    "div[class*='ReviewList'] div[class*='Review']",
    "div[class*='reviewCard']",
    "article[class*='review']",
  ]);
  reviewNodes.forEach(function (node) {
    var body =
      textOf(node.querySelector("[itemprop='reviewBody'], [class*='review-text'], [class*='comment'], p")) ||
      textOf(node);
    if (!body || body.length < 12) return;
    if (/^(ratings? & reviews|customer reviews|write a review)$/i.test(body)) return;
    var authorNode = node.querySelector("[itemprop='author'], [class*='user-name'], [class*='author'], [class*='name']");
    var dateNode = node.querySelector("[itemprop='datePublished'], [class*='date'], time");
    pushDoc({
      source: "ajio_manual",
      url: url,
      text: body,
      author: authorNode ? textOf(authorNode) : null,
      timestamp: dateNode ? dateNode.getAttribute("datetime") || textOf(dateNode) : null,
      meta: baseMeta("review", { rating: ratingFrom(node) }),
    });
  });

  var qaNodes = firstMatch([
    "div.qa-item",
    "li.qa-list-item",
    "div[class*='question'] div[class*='item']",
    "div[class*='Question']",
    "div[class*='qa-list'] > div",
  ]);
  qaNodes.forEach(function (node) {
    var raw = textOf(node);
    if (!raw) return;
    var qMatch = raw.match(/^\s*(?:q(?:uestion)?\s*[:.\)]\s*)?(.+?)(?:\s+a(?:nswer)?\s*[:.\)]\s*|$)/i);
    var question = textOf(node.querySelector("[class*='question'], [class*='ques']")) || (qMatch ? qMatch[1] : raw);
    question = question.replace(/^\s*(?:q|question)\s*[:.\)]\s*/i, "").trim();
    if (!question || question.length < 8) return;
    if (/^(questions?|q\s*&\s*a|ask a question)$/i.test(question)) return;
    var answers = [];
    node.querySelectorAll("[class*='answer'], [class*='ans']").forEach(function (ans) {
      var t = textOf(ans);
      if (t && t.length > 2) answers.push(t);
    });
    if (!answers.length) {
      var split = raw.split(/\s+A(?:nswer)?\s*[:.\)]\s*/i);
      if (split.length > 1) {
        question = split[0].replace(/^\s*(?:q|question)\s*[:.\)]\s*/i, "").trim();
        answers = split.slice(1).map(function (a) { return a.trim(); }).filter(Boolean);
      }
    }
    pushDoc({
      source: "ajio_manual",
      url: url,
      text: question,
      author: null,
      timestamp: null,
      meta: baseMeta("qa", { answers: answers, answer_count: answers.length }),
    });
  });

  // Last resort: whatever the person selected after scrolling.
  var selection = typeof window.getSelection === "function" ? String(window.getSelection()) : "";
  selection = selection.replace(/\s+/g, " ").trim();
  if (selection && selection.length >= 12 && documents.length === 0) {
    var looksLikeQuestion = /\?/.test(selection) || /^\s*q[:.]/i.test(selection);
    pushDoc({
      source: "ajio_manual",
      url: url,
      text: selection,
      author: null,
      timestamp: null,
      meta: baseMeta(looksLikeQuestion ? "qa" : "review"),
    });
    warnings.push(
      "fell back to the current text selection; set meta.content_type if it guessed wrong"
    );
  }

  if (!documents.length) {
    warnings.push(
      "nothing visible matched. Scroll Ratings & Reviews or Q&A into view, then run again. " +
        "A headless tab will usually be empty — this snippet is for a real session."
    );
  }

  var result = { documents: documents, warnings: warnings };
  if (typeof console !== "undefined" && console.log) {
    console.log("AJIO extract:", documents.length, "document(s)", warnings);
  }
  return result;
})();
