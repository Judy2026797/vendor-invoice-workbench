/* 供应商发票工作台 — 前端（仅供应商发票模块）
 * 角色：admin 全量读写 / staff 可录入(不可删/改付款) / 其他 只读
 * 硬阻断去重：相同 (发票号, 供应商) 不允许重复录入
 */
"use strict";

var CURRENT_USER = null;
var CURRENT_ROLE = null;
var ROLE_VENDOR_WRITE = ["admin", "staff"];   // 可录入
var ROLE_WRITE = ["admin"];                    // 可删/改付款状态
var editingId = null;                          // null = 新增
var rowsCache = [];                            // 当前列表缓存

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function api(method, path, body) {
  return new Promise(function (resolve, reject) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, path, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) {}
      resolve({ status: xhr.status, data: data });
    };
    xhr.send(body ? JSON.stringify(body) : null);
  });
}

/* ---------- 启动 ---------- */
function boot() {
  api("GET", "/api/me").then(function (r) {
    if (!r.data || !r.data.ok) { location.href = "/login.html"; return; }
    CURRENT_USER = r.data.user.username;
    CURRENT_ROLE = r.data.user.role;
    $("userBadge").textContent = CURRENT_USER + "（" + CURRENT_ROLE + "）";
    if (ROLE_VENDOR_WRITE.indexOf(CURRENT_ROLE) === -1) {
      // 只读：隐藏录入/导入按钮
      $("addBtn").style.display = "none";
      $("importBtn").style.display = "none";
    }
    load();
  });
}

function doLogout() {
  api("POST", "/api/logout").then(function () { location.href = "/login.html"; });
}

/* ---------- 列表 ---------- */
function load() {
  var q = $("q").value.trim();
  var v = $("fVendor").value.trim();
  var p = $("fPeriod").value.trim();
  var ps = $("fPayStatus").value;
  var url = "/api/vendor-invoices?q=" + encodeURIComponent(q) +
    "&vendor=" + encodeURIComponent(v) + "&period=" + encodeURIComponent(p) +
    "&pay_status=" + encodeURIComponent(ps);
  api("GET", url).then(function (r) {
    if (!r.data || !r.data.ok) return;
    rowsCache = r.data.rows || [];
    renderTable();
  });
}
var _t;
function debounceLoad() { clearTimeout(_t); _t = setTimeout(load, 300); }

function payBadge(s) {
  if (s === "已付") return '<span class="badge paid">已付</span>';
  if (s === "部分支付") return '<span class="badge partial">部分支付</span>';
  return '<span class="badge unpaid">未付</span>';
}

function renderTable() {
  var tb = $("tbody");
  if (!rowsCache.length) {
    tb.innerHTML = "";
    $("empty").style.display = "block";
    return;
  }
  $("empty").style.display = "none";
  var canWrite = ROLE_VENDOR_WRITE.indexOf(CURRENT_ROLE) !== -1;
  var canAdmin = ROLE_WRITE.indexOf(CURRENT_ROLE) !== -1;
  var html = "";
  rowsCache.forEach(function (r) {
    var acts = "";
    if (canWrite) {
      acts += '<a onclick="openForm(' + r.id + ')">编辑</a>';
      if (canAdmin) acts += '<a class="del" onclick="del(' + r.id + ')">删除</a>';
    } else {
      acts = '<span class="muted">只读</span>';
    }
    html +=
      "<tr>" +
      "<td class='mono'>" + esc(r.invoice_no) + "</td>" +
      "<td>" + esc(r.vendor) + "</td>" +
      "<td>" + esc(r.vendor_full) + "</td>" +
      "<td>" + esc(r.invoice_date) + "</td>" +
      "<td>" + esc(r.period) + "</td>" +
      "<td>" + esc(r.site) + "</td>" +
      "<td>" + esc(r.service) + "</td>" +
      "<td class='num'>" + fmt(r.ex_tax) + "</td>" +
      "<td class='num'>" + fmt(r.gst) + "</td>" +
      "<td class='num'>" + fmt(r.amount) + "</td>" +
      "<td>" + esc(r.currency) + "</td>" +
      "<td class='mono'>" + esc(r.bpm_no || "—") + "</td>" +
      "<td>" + payBadge(r.pay_status) + "</td>" +
      "<td class='mono'>" + esc(r.pay_no || "—") + "</td>" +
      "<td class='actions'>" + acts + "</td>" +
      "</tr>";
  });
  tb.innerHTML = html;
}
function fmt(n) {
  if (n == null || n === 0) return "0.00";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/* ---------- 录入 / 编辑 ---------- */
function openForm(id) {
  editingId = (id == null) ? null : id;
  $("reject").className = "reject";
  $("reject").innerHTML = "";
  $("formMsg").textContent = "";
  $("saveBtn").disabled = false;
  if (id == null) {
    $("formTitle").textContent = "新增供应商发票";
    ["f_invoice_no","f_vendor","f_vendor_full","f_invoice_date","f_period","f_site","f_service",
     "f_ex_tax","f_gst","f_amount","f_currency","f_bpm_no","f_note","f_pay_status","f_pay_no","f_pay_date","f_src"]
      .forEach(function (k) { $(k).value = (k === "f_currency") ? "INR" : (k === "f_pay_status" ? "未付" : ""); });
  } else {
    $("formTitle").textContent = "编辑供应商发票 #" + id;
    var r = rowsCache.filter(function (x) { return x.id === id; })[0];
    if (!r) return;
    $("f_invoice_no").value = r.invoice_no || "";
    $("f_vendor").value = r.vendor || "";
    $("f_vendor_full").value = r.vendor_full || "";
    $("f_invoice_date").value = r.invoice_date || "";
    $("f_period").value = r.period || "";
    $("f_site").value = r.site || "";
    $("f_service").value = r.service || "";
    $("f_ex_tax").value = r.ex_tax || 0;
    $("f_gst").value = r.gst || 0;
    $("f_amount").value = r.amount || 0;
    $("f_currency").value = r.currency || "INR";
    $("f_bpm_no").value = r.bpm_no || "";
    $("f_note").value = r.note || "";
    $("f_pay_status").value = r.pay_status || "未付";
    $("f_pay_no").value = r.pay_no || "";
    $("f_pay_date").value = r.pay_date || "";
    $("f_src").value = r.src || "";
  }
  // 付款字段门控：非 admin 只读
  var lockPay = ROLE_WRITE.indexOf(CURRENT_ROLE) === -1;
  ["f_pay_status","f_pay_no","f_pay_date"].forEach(function (k) { $(k).disabled = lockPay; });
  $("overlay").className = "overlay open";
}
function closeForm() { $("overlay").className = "overlay"; }

/* 录入时实时查重（仅新增态） */
function onKeyChange() {
  if (editingId != null) return; // 编辑态不拦截自身
  var no = $("f_invoice_no").value.trim();
  var v = $("f_vendor").value.trim();
  if (!no || !v) { $("reject").className = "reject"; $("reject").innerHTML = ""; $("saveBtn").disabled = false; return; }
  api("GET", "/api/vendor-invoice-check?invoice_no=" + encodeURIComponent(no) + "&vendor=" + encodeURIComponent(v))
    .then(function (r) {
      if (r.data && r.data.exists) {
        var e = (r.data.existing || [])[0] || {};
        $("reject").className = "reject show";
        $("reject").innerHTML = "⚠ 该发票已登记（重复，系统禁止录入）<br>" +
          "<b>现有记录：</b> #" + e.id + " · " + esc(e.vendor) + " · 发票号 " + esc(e.invoice_no) +
          " · 期间 " + esc(e.period) + " · BPM " + esc(e.bpm_no || "—") +
          " · 含税 " + fmt(e.amount) + " " + esc(e.currency);
        $("saveBtn").disabled = true;
      } else {
        $("reject").className = "reject"; $("reject").innerHTML = ""; $("saveBtn").disabled = false;
      }
    });
}

function collectInvoice() {
  return {
    invoice_no: $("f_invoice_no").value.trim(),
    vendor: $("f_vendor").value.trim(),
    vendor_full: $("f_vendor_full").value.trim(),
    invoice_date: $("f_invoice_date").value.trim(),
    period: $("f_period").value.trim(),
    site: $("f_site").value.trim(),
    service: $("f_service").value.trim(),
    ex_tax: parseFloat($("f_ex_tax").value || 0),
    gst: parseFloat($("f_gst").value || 0),
    amount: parseFloat($("f_amount").value || 0),
    currency: $("f_currency").value.trim().toUpperCase() || "INR",
    bpm_no: $("f_bpm_no").value.trim(),
    note: $("f_note").value.trim(),
    pay_status: $("f_pay_status").value,
    pay_no: $("f_pay_no").value.trim(),
    pay_date: $("f_pay_date").value.trim(),
    src: $("f_src").value.trim()
  };
}

function save() {
  var inv = collectInvoice();
  if (!inv.invoice_no || !inv.vendor) { $("formMsg").className = "msg err"; $("formMsg").textContent = "发票号与供应商必填"; return; }
  var body = { invoice: inv };
  if (editingId != null) body.invoice.id = editingId;
  $("saveBtn").disabled = true;
  api("POST", "/api/vendor-invoice", body).then(function (r) {
    $("saveBtn").disabled = false;
    if (!r.data) { $("formMsg").className = "msg err"; $("formMsg").textContent = "网络错误"; return; }
    if (r.data.duplicate) {
      var e = (r.data.existing || [])[0] || {};
      $("reject").className = "reject show";
      $("reject").innerHTML = "⚠ 该发票已登记（重复，系统禁止录入）<br><b>现有记录：</b> #" + e.id +
        " · " + esc(e.vendor) + " · " + esc(e.invoice_no) + " · " + esc(e.period);
      $("saveBtn").disabled = true;
      return;
    }
    if (!r.data.ok) { $("formMsg").className = "msg err"; $("formMsg").textContent = r.data.msg || "保存失败"; return; }
    closeForm();
    load();
  });
}

function del(id) {
  if (!confirm("确认删除发票 #" + id + "？此操作不可撤销。")) return;
  api("POST", "/api/vendor-invoice-delete", { id: id }).then(function (r) {
    if (r.data && r.data.ok) load();
    else alert((r.data && r.data.msg) || "删除失败");
  });
}

/* ---------- CSV 导入 / 导出 ---------- */
function exportCsv() {
  var cols = ["invoice_no","vendor","vendor_full","invoice_date","period","site","service",
    "ex_tax","gst","amount","currency","bpm_no","pay_status","pay_no","note"];
  var lines = [cols.join(",")];
  rowsCache.forEach(function (r) {
    lines.push(cols.map(function (c) {
      var v = r[c] == null ? "" : String(r[c]);
      return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }).join(","));
  });
  var blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "vendor_invoices.csv";
  a.click();
}

function importCsv(input) {
  var file = input.files[0];
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function () {
    var text = String(reader.result || "");
    var rows = parseCsv(text);
    if (rows.length < 2) { alert("CSV 无数据行"); return; }
    var header = rows[0].map(function (h) { return h.trim(); });
    var idx = {};
    header.forEach(function (h, i) { idx[h] = i; });
    var out = [];
    for (var i = 1; i < rows.length; i++) {
      var row = rows[i];
      function g(k) { return idx[k] != null && row[idx[k]] != null ? String(row[idx[k]]).trim() : ""; }
      out.push({
        invoice_no: g("invoice_no"), vendor: g("vendor"), vendor_full: g("vendor_full"),
        invoice_date: g("invoice_date"), period: g("period"), site: g("site"), service: g("service"),
        ex_tax: parseFloat(g("ex_tax") || 0), gst: parseFloat(g("gst") || 0),
        amount: parseFloat(g("amount") || 0), currency: g("currency") || "INR",
        bpm_no: g("bpm_no"), pay_status: g("pay_status") || "未付", pay_no: g("pay_no"), note: g("note"),
        src: "csv-import"
      });
    }
    api("POST", "/api/vendor-invoice-import", { rows: out }).then(function (r) {
      input.value = "";
      if (r.data && r.data.ok) {
        var msg = "导入完成：新增 " + r.data.inserted + "，跳过 " + r.data.skipped;
        if (r.data.duplicate_count) msg += "，重复拦截 " + r.data.duplicate_count + "（已阻止重复付款）";
        alert(msg);
        load();
      } else {
        alert((r.data && r.data.msg) || "导入失败");
      }
    });
  };
  reader.readAsText(file, "utf-8");
}

function parseCsv(text) {
  var rows = [], row = [], cur = "", q = false;
  for (var i = 0; i < text.length; i++) {
    var c = text[i];
    if (q) {
      if (c === '"') {
        if (text[i + 1] === '"') { cur += '"'; i++; } else q = false;
      } else cur += c;
    } else {
      if (c === '"') q = true;
      else if (c === ",") { row.push(cur); cur = ""; }
      else if (c === "\n") { row.push(cur); rows.push(row); row = []; cur = ""; }
      else if (c === "\r") { /* skip */ }
      else cur += c;
    }
  }
  if (cur.length || row.length) { row.push(cur); rows.push(row); }
  return rows;
}

boot();
