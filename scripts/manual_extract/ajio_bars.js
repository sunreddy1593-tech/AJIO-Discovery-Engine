// AJIO BARS — CONSOLE VERSION
// On an AJIO product page, scroll the "Ratings" / "Customer Opinion" section into
// view, then F12 -> Console -> paste this whole file -> Enter. It copies ONE
// aggregate record (numbers only, never a review) to your clipboard.
(function(){
    var out=((function extractAjioBars() {
    var VIA = (typeof window !== "undefined" && window.__AJIO_EXTRACT_VIA__) || "bookmarklet";
    var warnings = [];
    function txt(node) {
      if (!node) return "";
      var c = node.cloneNode(true);
      c.querySelectorAll("script,style,noscript,svg").forEach(function (e) { e.remove(); });
      return (c.innerText || c.textContent || "").replace(/\s+/g, " ").trim();
    }
    function pidFrom(h) { var m = String(h || "").match(/\/p\/(\d{6,})/); return m ? m[1] : null; }
    var canon = document.querySelector("link[rel='canonical']");
    var anyP = document.querySelector("a[href*='/p/']");
    var pid = pidFrom(location.href) || (canon && pidFrom(canon.href)) || (anyP && pidFrom(anyP.href)) || null;
    var titleEl = document.querySelector("h1, [class*='prod-name'], [class*='product-title']");
    var title = titleEl ? txt(titleEl) : null;
    var url = pid ? "https://www.ajio.com/p/" + pid : location.href.split("?")[0];
    if (!pid) warnings.push("no /p/<id> in the URL — set product_id by hand before saving");
  
    var average = null, count = null;
    document.querySelectorAll('script[type="application/ld+json"]').forEach(function (n) {
      var p; try { p = JSON.parse(n.textContent); } catch (e) { return; }
      var blocks = Array.isArray(p) ? p.slice() : [p];
      blocks.forEach(function (b) { if (b && Array.isArray(b["@graph"])) blocks.push.apply(blocks, b["@graph"]); });
      blocks.forEach(function (b) {
        if (!b || typeof b !== "object") return;
        var ar = b.aggregateRating;
        if (ar && typeof ar === "object") {
          if (average == null && ar.ratingValue != null) average = Number(ar.ratingValue);
          if (count == null && (ar.reviewCount != null || ar.ratingCount != null))
            count = Number(ar.reviewCount != null ? ar.reviewCount : ar.ratingCount);
        }
      });
    });
  
    var pageText = txt(document.body);
    // Count: the reliable anchor — "NN Customers" is present on every ratings block.
    var cm = pageText.match(/\d[\d,]*\s*Customers?/i);
    if (count == null && cm) { var mc = cm[0].match(/(\d[\d,]*)/); if (mc) count = Number(mc[1].replace(/,/g, "")); }
    // Average: AJIO's printed rating (e.g. "3.9") sits right next to that count. Anchor
    // off "Customers" and take the one-decimal value 1.0-5.0 in a window on either side —
    // order-independent, and far sturdier than the old "Ratings <n>" match which missed
    // AJIO's real layout on every product. A one-decimal in [1-5] can't collide with the
    // integer bucket/opinion percentages, so this stays clean.
    if (average == null && cm) {
      var w = pageText.slice(Math.max(0, cm.index - 40), Math.min(pageText.length, cm.index + cm[0].length + 15));
      var dm = w.match(/\b([1-5]\.\d)\b/);
      if (dm) average = Number(dm[1]);
    }
    if (average == null) warnings.push("average_rating not found on page — reader will derive it from rating_distribution");
  
    function parsePairs(seg) {
      var pairs = {}, pr = /([A-Za-z][A-Za-z ]*?)\s*\((\d{1,3})%\)/g, mm;
      while ((mm = pr.exec(seg))) { pairs[mm[1].trim().replace(/\s+/g, " ")] = Number(mm[2]); }
      return pairs;
    }
    var opinions = [], qRegex = /How was[^?]*\?/gi, questions = [], m;
    while ((m = qRegex.exec(pageText))) questions.push({ q: m[0].trim(), idx: m.index });
    for (var i = 0; i < questions.length; i++) {
      var start = questions[i].idx + questions[i].q.length;
      var end = (i + 1 < questions.length) ? questions[i + 1].idx : Math.min(pageText.length, start + 400);
      var pairs = parsePairs(pageText.slice(start, end));
      if (Object.keys(pairs).length) opinions.push({ question: questions[i].q, options: pairs });
    }
    if (!opinions.length) {
      var g = parsePairs(pageText);
      if (Object.keys(g).length) { opinions.push({ question: null, options: g }); warnings.push("found percentage options but no 'How was...?' headers; saved as one unlabeled panel — check it"); }
    }
  
    var distribution = {}, rdIdx = pageText.search(/Rating Distribution/i);
    if (rdIdx >= 0) {
      var rdSeg = pageText.slice(rdIdx, rdIdx + 300), rowRe = /([1-5])\D{0,8}?(\d{1,3})%/g, rm;
      while ((rm = rowRe.exec(rdSeg))) { if (distribution[rm[1]] == null) distribution[rm[1]] = Number(rm[2]); }
    }
    if (!Object.keys(distribution).length) warnings.push("no rating distribution parsed");
  
    var record = {
      source: "ajio_aggregate", product_id: pid, product_title: title, url: url,
      extraction: VIA, extracted_at: new Date().toISOString(),
      average_rating: average, rating_count: count,
      rating_distribution: distribution, opinions: opinions
    };
    if (average == null && count == null && !opinions.length && !Object.keys(distribution).length)
      warnings.push("nothing parsed — scroll the Ratings / Customer Opinion section into view, then run again");
    return { record: record, warnings: warnings };
  })());
    var r=out.record, w=out.warnings||[];
    function summarize(r){var s=(r.product_id||"?")+" | "+(r.average_rating!=null?r.average_rating:"?")+" ("+(r.rating_count!=null?r.rating_count:"?")+")";(r.opinions||[]).forEach(function(o){var top=Object.keys(o.options||{}).sort(function(a,b){return o.options[b]-o.options[a]})[0];if(top)s+=" | "+((o.question||"opinion").replace(/How was (the )?/i,"").replace(/\?$/,""))+": "+top+" "+o.options[top]+"%";});return s;}
    var json=JSON.stringify(r,null,2);
    try{copy(json);}catch(e){}
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(json).catch(function(){});}
    console.log("%cAJIO bars copied: "+summarize(r),"font-weight:bold;font-size:13px");
    if(w.length)console.warn("warnings:",w);
    return r;
  })();
  