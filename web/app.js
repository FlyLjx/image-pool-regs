const $ = (selector) => document.querySelector(selector)
const $$ = (selector) => [...document.querySelectorAll(selector)]

const state = {
  username: '',
  settings: null,
  jobs: {
    openai: null,
  },
  monitor: null,
  health: null,
  accounts: [],
  accountPage: 1,
  accountTargetPage: 1,
  accountPageSize: 20,
  accountPages: 1,
  accountTotal: 0,
  accountAllTotal: 0,
  accountSearch: '',
  accountCategory: 'all',
  accountSearchTimer: null,
  registrationDrafts: {},
  accountsLoading: false,
  accountReloadPending: false,
  logCursor: 0,
  visibleLogCounts: { openai: 0 },
  polling: false,
  pollTimer: null,
  pendingJobStarts: new Set(),
  pendingJobStops: new Set(),
  passwords: new Map(),
  outlookPool: null,
  outlookPoolItems: [],
  outlookPoolPage: 1,
  outlookPoolPageSize: 20,
  outlookPoolPages: 1,
  outlookPoolTotal: 0,
  outlookPoolSearch: '',
  outlookPoolStatus: 'all',
  outlookPoolSearchTimer: null,
  outlookPoolSelected: new Set(),
  outlookMails: null,
  outlookMailItems: [],
  outlookMailPage: 1,
  outlookMailPageSize: 20,
  outlookMailPages: 1,
  outlookMailTotal: 0,
  outlookMailSearch: '',
  outlookMailStatus: 'all',
  outlookMailSearchTimer: null,
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

function errorMessage(payload, fallback = '请求失败') {
  const detail = payload?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') return detail.error || JSON.stringify(detail)
  return fallback
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers })
  let payload = null
  const type = response.headers.get('content-type') || ''
  if (type.includes('application/json')) {
    payload = await response.json().catch(() => null)
  } else {
    payload = await response.text().catch(() => '')
  }
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/auth/login') showLogin()
    throw new Error(errorMessage(payload, `请求失败 HTTP ${response.status}`))
  }
  return payload
}

function toast(message, level = 'success') {
  const region = $('#toastRegion')
  const item = document.createElement('div')
  item.className = `toast ${level}`
  const icon = level === 'error' ? 'ti-alert-circle' : level === 'warning' ? 'ti-alert-triangle' : 'ti-circle-check'
  item.innerHTML = `<i class="ti ${icon}"></i><span>${escapeHtml(message)}</span>`
  region.appendChild(item)
  window.setTimeout(() => item.remove(), 3600)
}

function setBusy(button, busy, label = '') {
  if (!button) return
  if (busy) {
    button.dataset.original = button.innerHTML
    button.disabled = true
    button.innerHTML = `<i class="ti ti-loader-2"></i><span>${escapeHtml(label || '处理中')}</span>`
  } else {
    if (button.dataset.original) button.innerHTML = button.dataset.original
    button.disabled = false
  }
}

const registrationProviders = ['openai']

function resolveRegistrationProvider() { return 'openai' }

function registrationProviderName() { return 'ChatGPT' }

function registrationChannel(job = null) {
  const selected = document.querySelector('input[name="registrationChannel"]:checked')?.value
  const activeChannel = jobIsActive(job) ? job?.channel : ''
  return String(activeChannel || selected || state.settings?.registration?.channel || 'protocol').toLowerCase()
}

function providerModeLabel(_provider, job = null) {
  return registrationChannel(job) === 'browser' ? '浏览器模式' : '协议模式'
}

function emptyJob(provider) {
  return {
    provider: resolveRegistrationProvider(provider),
    state: 'idle',
    total: 0,
    success: 0,
    failed: 0,
    running: 0,
    elapsed_seconds: 0,
  }
}

function jobIsActive(job) {
  return ['running', 'stopping'].includes(job?.state)
}

function dashboardJobs(payload) {
  const received = payload?.jobs && typeof payload.jobs === 'object' ? payload.jobs : {}
  const legacy = payload?.job && typeof payload.job === 'object' ? payload.job : null
  return registrationProviders.reduce((jobs, provider) => {
    const supplied = received[provider]
    if (supplied && typeof supplied === 'object') {
      jobs[provider] = { ...emptyJob(provider), ...supplied, provider }
      return jobs
    }
    if (legacy && resolveRegistrationProvider(legacy.provider) === provider) {
      jobs[provider] = { ...emptyJob(provider), ...legacy, provider }
      return jobs
    }
    jobs[provider] = emptyJob(provider)
    return jobs
  }, {})
}

function jobFor(provider) {
  const resolved = resolveRegistrationProvider(provider)
  return state.jobs?.[resolved] || emptyJob(resolved)
}

function providerRegistrationSettings(registration, provider) {
  const resolved = resolveRegistrationProvider(provider)
  const providers = registration?.providers && typeof registration.providers === 'object'
    ? registration.providers
    : {}
  const specific = providers[resolved] || registration?.[resolved]
  return specific && typeof specific === 'object'
    ? { ...(registration || {}), ...specific }
    : registration || {}
}

function registrationInputs(provider) {
  const resolved = resolveRegistrationProvider(provider)
  return {
    count: $(`#${resolved}RunCount`),
    concurrency: $(`#${resolved}RunConcurrency`),
  }
}

function rememberRegistrationDraft(provider) {
  const resolved = resolveRegistrationProvider(provider)
  const inputs = registrationInputs(resolved)
  state.registrationDrafts[resolved] = {
    count: Number(inputs.count.value) || 1,
    concurrency: Number(inputs.concurrency.value) || 1,
  }
  return state.registrationDrafts[resolved]
}

function showLogin() {
  clearInterval(state.pollTimer)
  state.pollTimer = null
  $('#boot').hidden = true
  $('#appView').hidden = true
  $('#loginView').hidden = false
  $('#loginPassword').value = ''
}

async function showApp(username) {
  state.username = username
  $('#boot').hidden = true
  $('#loginView').hidden = true
  $('#appView').hidden = false
  $('#accountName').textContent = username
  $('#accountInitial').textContent = (username[0] || 'A').toUpperCase()
  $('#securityUsername').textContent = username
  await Promise.all([loadSettings(), loadOutlookPool(), loadOutlookMails(), loadDashboard(), loadLogs()])
  if (!state.pollTimer) state.pollTimer = window.setInterval(pollRuntime, 1500)
}

async function checkSession() {
  try {
    const session = await api('/api/auth/session')
    if (!session.authenticated) throw new Error('not authenticated')
    await showApp(session.username)
  } catch {
    showLogin()
  }
}

function setPage(page) {
  const target = ['register', 'outlook-pool', 'outlook-mails', 'settings'].includes(page) ? page : 'register'
  $$('.page').forEach((node) => node.classList.toggle('active', node.id === `page-${target}`))
  $$('[data-page]').forEach((node) => node.classList.toggle('active', node.dataset.page === target))
  if (location.hash !== `#${target}`) history.replaceState(null, '', `#${target}`)
  if (target === 'outlook-pool') loadOutlookPool().catch((error) => toast(error.message, 'error'))
  if (target === 'outlook-mails') loadOutlookMails().catch((error) => toast(error.message, 'error'))
}

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(value / 60).toString().padStart(2, '0')
  const rest = Math.floor(value % 60).toString().padStart(2, '0')
  return `${minutes}:${rest}`
}

function formatTime(value, timeOnly = false) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  if (timeOnly) return date.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return date.toLocaleString('zh-CN', { hour12: false })
}

function formatLifetime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0))
  if (value < 60) return `${value} 秒`
  if (value < 3600) return `${Math.floor(value / 60)} 分钟`
  if (value < 86400) return `${Math.floor(value / 3600)} 小时 ${Math.floor((value % 3600) / 60)} 分`
  return `${Math.floor(value / 86400)} 天 ${Math.floor((value % 86400) / 3600)} 小时`
}

function jobPresentation(job) {
  const map = {
    idle: ['空闲', 'neutral'],
    running: ['运行中', 'running'],
    stopping: ['停止中', 'warning'],
    stopped: ['已停止', 'warning'],
    skipped: ['已跳过', 'done'],
    completed: ['已完成', 'done'],
  }
  return map[job?.state] || ['空闲', 'neutral']
}

function jobMetaText(provider, job) {
  const base = `${registrationProviderName(provider)} · ${providerModeLabel(provider, job)}`
  if (job?.state === 'running') return `${base} · ${Number(job.running) || 0} 个执行中`
  if (job?.state === 'stopping') return `${base} · 正在停止`
  const message = String(job?.message || '').trim()
  if (message) return `${base} · ${message.slice(0, 42)}`
  return `${base} · 等待任务`
}

function renderProviderJob(provider, job) {
  const resolved = resolveRegistrationProvider(provider)
  const current = { ...emptyJob(resolved), ...(job || {}) }
  const active = jobIsActive(current)
  const finished = (Number(current.success) || 0) + (Number(current.failed) || 0)
  const total = Number(current.total) || 0
  const percent = total ? Math.min(100, Math.round((finished / total) * 100)) : 0
  const [label, tone] = jobPresentation(current)
  const prefix = 'openai'

  $(`#${prefix}JobMeta`).textContent = jobMetaText(resolved, current)
  const badge = $(`#${prefix}JobState`)
  badge.textContent = label
  badge.className = `status-badge ${tone}`
  $(`#${prefix}ProgressPercent`).textContent = `${percent}%`
  $(`#${prefix}ProgressBar`).style.width = `${percent}%`
  $(`#${prefix}ProgressSuccess`).textContent = Number(current.success) || 0
  $(`#${prefix}ProgressFailed`).textContent = Number(current.failed) || 0
  $(`#${prefix}ProgressElapsed`).textContent = formatDuration(current.elapsed_seconds)

  const startButton = $(`#${prefix}StartButton`)
  const forceButton = $(`#${prefix}ForceButton`)
  const stopButton = $(`#${prefix}StopButton`)
  const countInput = $(`#${prefix}RunCount`)
  const concurrencyInput = $(`#${prefix}RunConcurrency`)
  const pendingStart = state.pendingJobStarts.has(resolved)
  const pendingStop = state.pendingJobStops.has(resolved)
  startButton.disabled = active || pendingStart
  if (forceButton) forceButton.disabled = active || pendingStart
  stopButton.disabled = !active || current.state === 'stopping' || pendingStop
  countInput.disabled = active
  concurrencyInput.disabled = active
  $$('input[name="registrationChannel"]').forEach((input) => { input.disabled = active })
}

function renderDashboard(payload) {
  const monitor = payload.monitor || state.monitor || {}
  const health = payload.health || state.health || {}
  const accounts = payload.accounts || {}
  const previousJobs = state.jobs || {}
  const previousHealthState = state.health?.state
  const jobs = dashboardJobs(payload)
  state.jobs = jobs
  state.monitor = monitor
  state.health = health
  $('#statTotal').textContent = accounts.total || 0
  $('#statToday').textContent = accounts.today || 0
  const sideAccountCount = $('#sideAccountCount')
  if (sideAccountCount) sideAccountCount.textContent = accounts.total || 0
  const finished = registrationProviders.reduce((sum, provider) => {
    const job = jobs[provider]
    return sum + (Number(job.success) || 0) + (Number(job.failed) || 0)
  }, 0)
  const total = registrationProviders.reduce((sum, provider) => sum + (Number(jobs[provider].total) || 0), 0)
  const failed = registrationProviders.reduce((sum, provider) => sum + (Number(jobs[provider].failed) || 0), 0)
  const activeProviders = registrationProviders.filter((provider) => jobIsActive(jobs[provider]))
  $('#statFailed').textContent = failed
  $('#statProgress').textContent = `${finished} / ${total}`
  $('#statProgressMeta').textContent = activeProviders.length
    ? `${activeProviders.map(registrationProviderName).join('、')} 任务运行中`
    : total ? '最近批次汇总' : '等待任务'
  registrationProviders.forEach((provider) => renderProviderJob(provider, jobs[provider]))

  const topBadge = $('#topJobBadge')
  const stoppingOnly = activeProviders.length > 0 && activeProviders.every((provider) => jobs[provider].state === 'stopping')
  const topLabel = activeProviders.length
    ? `${activeProviders.map(registrationProviderName).join('、')}${stoppingOnly ? ' 停止中' : ' 运行中'}`
    : '空闲'
  topBadge.innerHTML = `<i class="ti ti-circle-dot"></i>${escapeHtml(topLabel)}`
  topBadge.className = `status-badge ${activeProviders.length ? (stoppingOnly ? 'warning' : 'running') : 'neutral'}`
  const monitorEnabled = Boolean(monitor.enabled)
  $('#monitorState').textContent = monitorEnabled ? '监听中' : '未开启'
  $('#monitorState').className = monitorEnabled ? 'active' : ''
  $('#monitorMeta').textContent = monitor.message || (monitorEnabled ? '等待容量检查' : '自动监听未开启')
  const monitorButton = $('#monitorButton')
  monitorButton.innerHTML = monitorEnabled
    ? '<i class="ti ti-radar-off"></i><span>停止监听</span>'
    : '<i class="ti ti-radar"></i><span>开启监听</span>'
  monitorButton.classList.toggle('danger', monitorEnabled)
  monitorButton.classList.toggle('secondary', !monitorEnabled)
  const completedProvider = registrationProviders.some((provider) => jobIsActive(previousJobs[provider]) && !jobIsActive(jobs[provider]))
  const healthRunning = ['running', 'stopping'].includes(health.state)
  const healthStopping = health.state === 'stopping'
  const healthButton = $('#healthCheckAllButton')
  healthButton.disabled = healthStopping
  healthButton.innerHTML = health.state === 'running'
    ? '<i class="ti ti-list-check"></i><span>追加检测全部</span>'
    : '<i class="ti ti-heartbeat"></i><span>检测全部</span>'
  const healthStopButton = $('#healthStopButton')
  healthStopButton.hidden = !healthRunning
  healthStopButton.disabled = healthStopping
  healthStopButton.innerHTML = healthStopping
    ? '<i class="ti ti-loader-2"></i><span>停止中</span>'
    : '<i class="ti ti-player-stop"></i><span>停止检测/恢复</span>'
  const healthTotal = Number(health.total) || 0
  const healthChecked = Number(health.checked) || 0
  const healthPercent = healthTotal ? Math.min(100, Math.round((healthChecked / healthTotal) * 100)) : 0
  const liveBar = $('#healthLiveBar')
  liveBar.hidden = healthTotal === 0
  liveBar.classList.toggle('running', healthRunning)
  $('#healthLiveTitle').textContent = healthStopping
    ? '正在停止检测/恢复'
    : health.state === 'running'
      ? '实时检测中'
      : health.state === 'cancelled'
        ? '本轮检测/恢复已停止'
        : '最近检测结果'
  const healthProbed = Number(health.probed) || healthChecked
  $('#healthLiveMeta').textContent = `${healthChecked} / ${healthTotal} · 已探测 ${healthProbed}${health.current_email ? ` · ${health.current_email}` : ''}`
  $('#healthLiveProgress').style.width = `${healthPercent}%`
  $('#healthLiveAlive').textContent = `存活 ${health.alive || 0}`
  $('#healthLiveRecovered').textContent = `恢复成功 ${health.recovered || 0}`
  const recoveryTotal = Number(health.recovery_total) || 0
  const recoveryCompleted = Number(health.recovery_completed) || 0
  const recoveryActive = Number(health.recovery_active) || 0
  const recoveryWaiting = Number(health.recovery_waiting) || 0
  const recoveryQueue = $('#healthLiveQueue')
  recoveryQueue.hidden = recoveryTotal === 0
  recoveryQueue.textContent = health.state === 'cancelled'
    ? `恢复已停止 ${recoveryCompleted} / ${recoveryTotal}`
    : `恢复中 ${recoveryActive} · 等待 ${recoveryWaiting} · 尝试完成 ${recoveryCompleted}/${recoveryTotal}`
  renderRecoveryProgress(health)
  $('#healthLiveBanned').textContent = `封禁 ${health.banned || 0}`
}

function renderRecoveryProgress(health) {
  const panel = $('#recoveryProgressPanel')
  const items = Array.isArray(health.recovery_items) ? health.recovery_items : []
  const active = Number(health.recovery_active) || 0
  const show = ['running', 'stopping'].includes(health.state) && (active > 0 || items.length > 0)
  panel.hidden = !show
  if (!show) return

  const stages = {
    starting: ['启动恢复', 'ti-player-play', 'neutral'],
    oauth: ['初始化 OAuth', 'ti-key', 'blue'],
    challenge: ['处理 Cloudflare', 'ti-cloud-lock', 'amber'],
    password: ['提交密码', 'ti-lock', 'blue'],
    otp: ['等待/校验验证码', 'ti-mail-code', 'amber'],
    profile: ['补全资料', 'ti-user-edit', 'blue'],
    refresh: ['刷新 Token', 'ti-refresh', 'green'],
    token: ['换取 Token', 'ti-arrows-exchange', 'green'],
    verify: ['验证新 Token', 'ti-shield-check', 'green'],
  }
  $('#recoveryProgressTitle').textContent = `正在恢复 ${active} 个账号`
  const counts = health.recovery_stage_counts || {}
  $('#recoveryStageCounts').innerHTML = Object.entries(counts)
    .filter(([, count]) => Number(count) > 0)
    .map(([stage, count]) => {
      const meta = stages[stage] || stages.starting
      return `<span class="${meta[2]}"><i class="ti ${meta[1]}"></i>${escapeHtml(meta[0])}<b>${Number(count)}</b></span>`
    })
    .join('')
  $('#recoveryActiveList').innerHTML = items.length
    ? items.map((item) => {
      const meta = stages[item.stage] || stages.starting
      return `<div class="recovery-active-row"><span class="recovery-account"><strong title="${escapeHtml(item.email)}">${escapeHtml(item.email || '-')}</strong><small>${escapeHtml(item.message || item.stage_label || meta[0])}</small></span><em class="${meta[2]}"><i class="ti ${meta[1]}"></i>${escapeHtml(item.stage_label || meta[0])}</em><time>${escapeHtml(formatTime(item.updated_at))}</time></div>`
    }).join('')
    : '<div class="recovery-active-empty">正在分配恢复任务</div>'
}

async function loadDashboard() {
  const payload = await api('/api/dashboard')
  renderDashboard(payload)
  return payload
}

function appendLogs(items) {
  if (!items?.length) return
  const grouped = { openai: items }
  Object.entries(grouped).forEach(([provider, providerItems]) => {
    if (!providerItems.length) return
    const list = $(`#${provider}LogList`)
    if (list.querySelector('.log-empty')) list.innerHTML = ''
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 55
    const fragment = document.createDocumentFragment()
    providerItems.forEach((item) => {
      const row = document.createElement('div')
      row.className = `log-row ${item.level || 'info'}`
      const prefix = item.provider === 'openai' ? 'GPT' : 'SYS'
      const source = item.task ? `${prefix}#${escapeHtml(item.task)}` : prefix
      row.innerHTML = `<time>${escapeHtml(formatTime(item.time, true))}</time><em>${source}</em><span>${escapeHtml(item.message)}</span>`
      fragment.appendChild(row)
    })
    list.appendChild(fragment)
    state.visibleLogCounts[provider] += providerItems.length
    $(`#${provider}LogCount`).textContent = `${state.visibleLogCounts[provider]} 条`
    if (nearBottom) list.scrollTop = list.scrollHeight
  })
}

async function loadLogs() {
  const payload = await api(`/api/logs?cursor=${state.logCursor}`)
  state.logCursor = payload.cursor || state.logCursor
  appendLogs(payload.items || [])
}

async function pollRuntime() {
  if (state.polling || $('#appView').hidden) return
  state.polling = true
  try {
    await Promise.all([loadDashboard(), loadLogs()])
  } catch (error) {
    if (!$('#appView').hidden) console.error(error)
  } finally {
    state.polling = false
  }
}

function fillSettings(settings) {
  state.settings = settings
  const registration = settings.registration || {}
  const mail = settings.mail || {}
  const cloud = settings.cloud || {}
  const notifications = settings.notifications || {}
  const flare = settings.flaresolverr || {}
  const sentinel = settings.sentinel || {}
  const health = settings.health || {}
  const openaiRegistration = providerRegistrationSettings(registration, 'openai')
  const openaiDraft = state.registrationDrafts.openai || {}
  if (!jobIsActive(jobFor('openai'))) {
    $('#openaiRunCount').value = openaiDraft.count || openaiRegistration.count || registration.count || 1
    $('#openaiRunConcurrency').value = openaiDraft.concurrency || openaiRegistration.concurrency || registration.concurrency || 1
  }
  const channel = registration.channel || 'protocol'
  const channelInput = document.querySelector(`input[name="registrationChannel"][value="${channel}"]`)
  if (channelInput) channelInput.checked = true
  $('#mailApiBase').value = mail.api_base || ''
  $('#mailApiKey').value = mail.api_key || ''
  $('#mailDomains').value = (mail.domains || ['auto']).join(', ')
  $('#mailPrefix').value = mail.email_prefix || ''
  $('#mailProvider').value = mail.provider || 'yyds'
  $('#outlookSplitLimit').value = mail.outlook_split_limit ?? 5
  $('#cloudEnabled').checked = Boolean(cloud.enabled)
  $('#cloudServer').value = cloud.server || ''
  $('#cloudAuthKey').value = cloud.auth_key || ''
  $('#cloudCapacityLimit').value = cloud.capacity_limit || 60
  $('#cloudUseCapacity').checked = cloud.use_capacity !== false
  $('#cloudUploadAccounts').checked = cloud.upload_accounts !== false
  $('#cloudUseProxy').checked = cloud.use_proxy !== false
  $('#cloudMonitorEnabled').checked = Boolean(cloud.monitor_enabled)
  $('#cloudMonitorInterval').value = cloud.monitor_interval_seconds || 30
  $('#cloudMonitorConcurrency').value = cloud.monitor_concurrency || 5
  $('#cloudShortageConfirmations').value = cloud.shortage_confirmations || 2
  $('#cloudMonitorBatchLimit').value = cloud.monitor_batch_limit || 20
  $('#barkEnabled').checked = Boolean(notifications.bark_enabled)
  $('#barkUrl').value = notifications.bark_url || 'https://api.day.app'
  $('#barkKey').value = notifications.bark_key || ''
  $('#barkLowStockThreshold').value = notifications.bark_low_stock_threshold || 100
  $('#barkCheckInterval').value = notifications.bark_check_interval_seconds || 30
  $('#barkReportEnabled').checked = Boolean(notifications.bark_report_enabled)
  $('#barkReportInterval').value = notifications.bark_report_interval_seconds || 3600
  $('#proxyUrl').value = registration.proxy || ''
  $('#browserProfile').value = registration.browser_profile || 'chrome_windows'
  $('#browserEngine').value = registration.browser_engine || 'camoufox'
  $('#browserHeadless').checked = Boolean(registration.browser_headless)
  $('#browserSlowMo').value = registration.browser_slow_mo_ms ?? 40
  $('#requestTimeout').value = registration.request_timeout || 45
  $('#mailWaitTimeout').value = registration.mail_wait_timeout || 120
  $('#mailPollInterval').value = registration.mail_poll_interval || 3
  $('#sentinelEnabled').checked = sentinel.so_enabled !== false
  $('#sentinelRequired').checked = Boolean(sentinel.so_required)
  $('#flareEnabled').checked = Boolean(flare.enabled)
  $('#flareUrl').value = flare.url || 'http://flaresolverr:8191'
  $('#flarePassProxy').checked = Boolean(flare.pass_proxy)
  $('#healthAutoEnabled').checked = Boolean(health.auto_check_enabled)
  $('#healthInterval').value = health.interval_seconds || 300
  $('#healthConcurrency').value = health.concurrency || 3
  $('#healthRecoveryConcurrency').value = health.recovery_concurrency || 3
  $('#healthRequestTimeout').value = health.request_timeout || 30
}

async function loadSettings() {
  const settings = await api('/api/settings')
  fillSettings(settings)
  return settings
}

function collectSettings() {
  const domains = $('#mailDomains').value.split(',').map((value) => value.trim()).filter(Boolean)
  const openaiRegistration = rememberRegistrationDraft('openai')
  return {
    registration: {
      count: openaiRegistration.count,
      concurrency: openaiRegistration.concurrency,
      channel: registrationChannel(),
      providers: {
        openai: openaiRegistration,
      },
      proxy: $('#proxyUrl').value.trim(),
      browser_profile: $('#browserProfile').value,
      browser_engine: $('#browserEngine').value,
      browser_headless: $('#browserHeadless').checked,
      browser_slow_mo_ms: Number($('#browserSlowMo').value),
      request_timeout: Number($('#requestTimeout').value),
      mail_wait_timeout: Number($('#mailWaitTimeout').value),
      mail_poll_interval: Number($('#mailPollInterval').value),
    },
    mail: {
      provider: $('#mailProvider').value,
      api_base: $('#mailApiBase').value.trim(),
      api_key: $('#mailApiKey').value.trim(),
      domains: domains.length ? domains : ['auto'],
      email_prefix: $('#mailPrefix').value.trim(),
      outlook_split_limit: Number($('#outlookSplitLimit').value),
    },
    cloud: {
      enabled: $('#cloudEnabled').checked,
      server: $('#cloudServer').value.trim(),
      auth_key: $('#cloudAuthKey').value.trim(),
      use_capacity: $('#cloudUseCapacity').checked,
      capacity_limit: Number($('#cloudCapacityLimit').value),
      upload_accounts: $('#cloudUploadAccounts').checked,
      use_proxy: $('#cloudUseProxy').checked,
      monitor_enabled: $('#cloudMonitorEnabled').checked,
      monitor_interval_seconds: Number($('#cloudMonitorInterval').value),
      monitor_concurrency: Number($('#cloudMonitorConcurrency').value),
      shortage_confirmations: Number($('#cloudShortageConfirmations').value),
      monitor_batch_limit: Number($('#cloudMonitorBatchLimit').value),
    },
    notifications: {
      bark_enabled: $('#barkEnabled').checked,
      bark_url: $('#barkUrl').value.trim(),
      bark_key: $('#barkKey').value.trim(),
      bark_low_stock_threshold: Number($('#barkLowStockThreshold').value),
      bark_check_interval_seconds: Number($('#barkCheckInterval').value),
      bark_report_enabled: $('#barkReportEnabled').checked,
      bark_report_interval_seconds: Number($('#barkReportInterval').value),
    },
    flaresolverr: {
      enabled: $('#flareEnabled').checked,
      url: $('#flareUrl').value.trim(),
      max_timeout_ms: state.settings?.flaresolverr?.max_timeout_ms || 60000,
      pass_proxy: $('#flarePassProxy').checked,
    },
    sentinel: {
      so_enabled: $('#sentinelEnabled').checked,
      so_required: $('#sentinelRequired').checked,
      node: state.settings?.sentinel?.node || 'node',
      timeout_ms: state.settings?.sentinel?.timeout_ms || 75000,
    },
    health: {
      auto_check_enabled: $('#healthAutoEnabled').checked,
      interval_seconds: Number($('#healthInterval').value),
      concurrency: Number($('#healthConcurrency').value),
      recovery_concurrency: Number($('#healthRecoveryConcurrency').value),
      request_timeout: Number($('#healthRequestTimeout').value),
    },
  }
}

function healthPresentation(account) {
  const status = String(account.health_status || 'unchecked').toLowerCase()
  if (status === 'alive' && account.health_recovery_status === 'recovered') return ['已恢复', 'recovered', 'ti-refresh-check']
  const values = {
    alive: ['存活', 'alive', 'ti-heart-check'],
    expired: ['失效', 'expired', 'ti-clock-x'],
    banned: ['封禁', 'banned', 'ti-ban'],
    restricted: ['受限', 'restricted', 'ti-lock'],
    environment: ['环境验证', 'environment', 'ti-shield-exclamation'],
    recovering: ['恢复中', 'recovering', 'ti-refresh'],
    rate_limited: ['限流', 'restricted', 'ti-hourglass'],
    unknown: ['异常', 'unknown', 'ti-help-circle'],
    checking: ['检测中', 'checking', 'ti-loader-2'],
    unchecked: ['未检测', 'unchecked', 'ti-circle-dashed'],
  }
  return values[status] || values.unchecked
}

function recoveryPresentation(account) {
  const status = String(account.health_recovery_status || '').toLowerCase()
  const healthStatus = String(account.health_status || '').toLowerCase()
  const detail = String(account.health_detail || '').trim()
  const values = {
    queued: ['等待恢复', 'queued', 'ti-clock'],
    running: [detail || '正在恢复账号', 'running', 'ti-loader-2'],
    failed: [detail || '恢复失败', 'failed', 'ti-alert-circle'],
    missing_credentials: ['缺少邮箱或密码', 'failed', 'ti-key-off'],
    token_refreshed: [detail || 'Token 已刷新，等待验证', 'queued', 'ti-refresh'],
    recovered: ['恢复成功', 'success', 'ti-refresh-check'],
    stopped: ['本轮恢复已停止', 'stopped', 'ti-player-stop'],
    interrupted: ['上次恢复被中断', 'stopped', 'ti-player-pause'],
  }
  if (values[status]) return values[status]
  if (healthStatus === 'expired') return ['等待恢复', 'queued', 'ti-clock']
  return null
}

function accountPageItems(currentPage, totalPages) {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1)
  let start = Math.max(2, currentPage - 1)
  let end = Math.min(totalPages - 1, currentPage + 1)
  if (currentPage <= 3) end = Math.min(totalPages - 1, 5)
  if (currentPage >= totalPages - 2) start = Math.max(2, totalPages - 4)
  const items = [1]
  if (start > 2) items.push('ellipsis-start')
  for (let page = start; page <= end; page += 1) items.push(page)
  if (end < totalPages - 1) items.push('ellipsis-end')
  items.push(totalPages)
  return items
}

function renderAccountPagination() {
  const firstItem = state.accountTotal ? ((state.accountPage - 1) * state.accountPageSize) + 1 : 0
  const lastItem = Math.min(state.accountPage * state.accountPageSize, state.accountTotal)
  $('#accountsPageSummary').textContent = `第 ${state.accountPage} / ${state.accountPages} 页`
  $('#accountsRangeSummary').textContent = state.accountTotal
    ? `显示 ${firstItem}-${lastItem}，共 ${state.accountTotal} 个账号`
    : '暂无账号'
  $('#accountsPageSize').value = String(state.accountPageSize)
  $('#accountsPrevPage').disabled = state.accountPage <= 1
  $('#accountsNextPage').disabled = state.accountPage >= state.accountPages
  $('#accountsPageNumbers').innerHTML = accountPageItems(state.accountPage, state.accountPages)
    .map((item) => typeof item === 'number'
      ? `<button class="pagination-page${item === state.accountPage ? ' active' : ''}" type="button" data-account-page="${item}" aria-label="第 ${item} 页"${item === state.accountPage ? ' aria-current="page"' : ''}>${item}</button>`
      : '<span class="pagination-ellipsis" aria-hidden="true">...</span>')
    .join('')
}

async function saveSettings() {
  const button = $('#saveSettingsButton')
  setBusy(button, true, '保存中')
  try {
    const saved = await api('/api/settings', { method: 'PUT', body: JSON.stringify(collectSettings()) })
    fillSettings(saved)
    await loadOutlookPool()
    toast('设置已保存')
  } finally {
    setBusy(button, false)
  }
}

function outlookPoolStatusPresentation(status) {
  const map = {
    available: ['可用', 'alive', 'ti-circle-check'],
    leased: ['租用中', 'recovering', 'ti-loader-2'],
    used: ['已用完', 'unchecked', 'ti-lock'],
    failed: ['异常', 'banned', 'ti-alert-circle'],
  }
  return map[status] || ['未知', 'unchecked', 'ti-help-circle']
}

function updateOutlookPoolSelectionUi() {
  const pageIds = state.outlookPoolItems.map((item) => String(item.id || '')).filter(Boolean)
  const selectedOnPage = pageIds.filter((id) => state.outlookPoolSelected.has(id)).length
  const selectPage = $('#outlookPoolSelectPage')
  selectPage.checked = pageIds.length > 0 && selectedOnPage === pageIds.length
  selectPage.indeterminate = selectedOnPage > 0 && selectedOnPage < pageIds.length
  selectPage.disabled = pageIds.length === 0
  const deleteButton = $('#deleteSelectedOutlookButton')
  deleteButton.disabled = state.outlookPoolSelected.size === 0
  deleteButton.querySelector('span').textContent = state.outlookPoolSelected.size
    ? `删除所选 (${state.outlookPoolSelected.size})`
    : '删除所选'
  $('#clearOutlookPoolButton').disabled = Number(state.outlookPool?.summary?.total || 0) === 0
  $('#deleteFailedOutlookButton').disabled = Number(state.outlookPool?.summary?.failed || 0) === 0
  $$('[data-outlook-select-id]').forEach((input) => {
    const selected = state.outlookPoolSelected.has(input.dataset.outlookSelectId)
    input.checked = selected
    input.closest('tr')?.classList.toggle('selected', selected)
  })
}

function renderOutlookPoolPagination() {
  const firstItem = state.outlookPoolTotal ? ((state.outlookPoolPage - 1) * state.outlookPoolPageSize) + 1 : 0
  const lastItem = Math.min(state.outlookPoolPage * state.outlookPoolPageSize, state.outlookPoolTotal)
  $('#outlookPoolPageSummary').textContent = `第 ${state.outlookPoolPage} / ${state.outlookPoolPages} 页`
  $('#outlookPoolRangeSummary').textContent = state.outlookPoolTotal
    ? `显示 ${firstItem}-${lastItem}，共 ${state.outlookPoolTotal} 个邮箱`
    : '暂无邮箱'
  $('#outlookPoolPageSize').value = String(state.outlookPoolPageSize)
  $('#outlookPoolPrevPage').disabled = state.outlookPoolPage <= 1
  $('#outlookPoolNextPage').disabled = state.outlookPoolPage >= state.outlookPoolPages
  $('#outlookPoolPageNumbers').innerHTML = accountPageItems(state.outlookPoolPage, state.outlookPoolPages)
    .map((item) => typeof item === 'number'
      ? `<button class="pagination-page${item === state.outlookPoolPage ? ' active' : ''}" type="button" data-outlook-page="${item}" aria-label="第 ${item} 页"${item === state.outlookPoolPage ? ' aria-current="page"' : ''}>${item}</button>`
      : '<span class="pagination-ellipsis" aria-hidden="true">...</span>')
    .join('')
}

function renderOutlookPool(payload) {
  state.outlookPool = payload || null
  const pool = payload?.summary || payload || {}
  const total = Number(pool.total) || 0
  const slots = Number(pool.available_slots) || 0
  const available = Number(pool.available) || 0
  const leased = Number(pool.leased) || 0
  const used = Number(pool.used) || 0
  const failed = Number(pool.failed) || 0
  const split = Number(pool.split_limit) || Number($('#outlookSplitLimit').value) || 5
  $('#outlookPoolStatus').textContent = `基础邮箱 ${total} · 可用注册 ${slots} · 母号 + ${split} 分裂 · 已用 ${used}${failed ? ` · 异常 ${failed}` : ''}`
  $('#sideOutlookSlotCount').textContent = slots
  $('#outlookStatTotal').textContent = total
  $('#outlookStatSlots').textContent = slots
  $('#outlookStatSplitLimit').textContent = `母号 + ${split} 分裂`
  $('#outlookStatOccupied').textContent = leased + used
  $('#outlookStatOccupiedMeta').textContent = `租用 ${leased} · 已用 ${used}`
  $('#outlookStatFailed').textContent = failed

  if (!Array.isArray(payload?.items)) return
  state.outlookPoolItems = payload.items
  state.outlookPoolPage = Number(payload.page) || 1
  state.outlookPoolPageSize = Number(payload.page_size) || state.outlookPoolPageSize
  state.outlookPoolPages = Number(payload.pages) || 1
  state.outlookPoolTotal = Number(payload.total) || 0
  state.outlookPoolStatus = payload.status || state.outlookPoolStatus
  $('#outlookPoolListMeta').textContent = state.outlookPoolSearch || state.outlookPoolStatus !== 'all'
    ? `${state.outlookPoolTotal} 条匹配 · 全部 ${total}`
    : `${total} 个基础邮箱`
  const statusCounts = { all: total, available, leased, used, failed }
  $$('[data-outlook-status]').forEach((button) => {
    const category = button.dataset.outlookStatus
    const active = category === state.outlookPoolStatus
    button.classList.toggle('active', active)
    button.setAttribute('aria-selected', active ? 'true' : 'false')
    const count = button.querySelector('b')
    if (count) count.textContent = statusCounts[category] || 0
  })

  const importApi = payload.import_api || {}
  $('#outlookImportEndpoint').value = importApi.endpoint ? `${location.origin}${importApi.endpoint}` : ''
  $('#outlookImportApiKey').value = importApi.api_key || ''
  renderOutlookPoolPagination()
  const body = $('#outlookPoolBody')
  const empty = $('#outlookPoolEmpty')
  body.innerHTML = ''
  empty.hidden = state.outlookPoolItems.length > 0
  empty.querySelector('strong').textContent = state.outlookPoolSearch || state.outlookPoolStatus !== 'all'
    ? '没有匹配的 Outlook 邮箱'
    : '暂无 Outlook 邮箱'
  state.outlookPoolItems.forEach((item) => {
    const [label, tone, icon] = outlookPoolStatusPresentation(item.status)
    const occupied = Number(item.used_splits) || 0
    const activeLeases = Number(item.leased_splits) || 0
    const detail = item.last_error || '状态正常'
    const row = document.createElement('tr')
    row.innerHTML = `
      <td class="outlook-select-cell"><input type="checkbox" data-outlook-select-id="${escapeHtml(item.id)}" aria-label="选择 ${escapeHtml(item.email || 'Outlook 邮箱')}"></td>
      <td class="email-cell" title="${escapeHtml(item.email)}"><span>${escapeHtml(item.email || '-')}</span></td>
      <td><span class="health-state ${tone}" title="${escapeHtml(detail)}"><i class="ti ${icon}"></i>${escapeHtml(label)}</span></td>
      <td><span class="outlook-split-usage"><strong>${occupied}</strong> 已用${activeLeases ? `<em>${activeLeases} 租用</em>` : ''}<small>/ ${(Number(item.split_limit) || split) + 1}</small></span></td>
      <td><strong class="outlook-available-count">${Number(item.available_splits) || 0}</strong></td>
      <td><time title="${escapeHtml(item.imported_at || '')}">${escapeHtml(formatTime(item.imported_at))}</time></td>
      <td class="token-cell" title="${escapeHtml(item.client_id)}">${escapeHtml(item.client_id || '-')}</td>
      <td><div class="outlook-update-cell"><time>${escapeHtml(formatTime(item.updated_at))}</time><small class="${item.last_error ? 'error' : ''}" title="${escapeHtml(detail)}">${escapeHtml(detail)}</small></div></td>`
    body.appendChild(row)
  })
  updateOutlookPoolSelectionUi()
}

async function loadOutlookPool(page = null) {
  const requestedPage = Math.max(1, Number(page ?? state.outlookPoolPage) || 1)
  const params = new URLSearchParams({
    page: String(requestedPage),
    page_size: String(state.outlookPoolPageSize),
    status: state.outlookPoolStatus,
  })
  if (state.outlookPoolSearch) params.set('query', state.outlookPoolSearch)
  const payload = await api(`/api/outlook-pool?${params.toString()}`)
  renderOutlookPool(payload)
  return payload
}

function renderOutlookMailPagination() {
  const firstItem = state.outlookMailTotal ? ((state.outlookMailPage - 1) * state.outlookMailPageSize) + 1 : 0
  const lastItem = Math.min(state.outlookMailPage * state.outlookMailPageSize, state.outlookMailTotal)
  $('#outlookMailPageSummary').textContent = `第 ${state.outlookMailPage} / ${state.outlookMailPages} 页`
  $('#outlookMailRangeSummary').textContent = state.outlookMailTotal
    ? `显示 ${firstItem}-${lastItem}，共 ${state.outlookMailTotal} 个邮箱`
    : '暂无邮箱'
  $('#outlookMailPageSize').value = String(state.outlookMailPageSize)
  $('#outlookMailPrevPage').disabled = state.outlookMailPage <= 1
  $('#outlookMailNextPage').disabled = state.outlookMailPage >= state.outlookMailPages
  $('#outlookMailPageNumbers').innerHTML = accountPageItems(state.outlookMailPage, state.outlookMailPages)
    .map((item) => typeof item === 'number'
      ? `<button class="pagination-page${item === state.outlookMailPage ? ' active' : ''}" type="button" data-outlook-mail-page="${item}" aria-label="第 ${item} 页"${item === state.outlookMailPage ? ' aria-current="page"' : ''}>${item}</button>`
      : '<span class="pagination-ellipsis" aria-hidden="true">...</span>')
    .join('')
}

function renderOutlookMailImportStats(stats) {
  const container = $('#outlookMailImportStats')
  const recent = Array.isArray(stats?.recent) ? stats.recent : []
  if (!recent.length) {
    container.innerHTML = '<div class="log-empty"><i class="ti ti-chart-bar"></i><span>暂无导入记录</span></div>'
    return
  }
  container.innerHTML = recent.slice(0, 14).map((item) => `
    <div class="outlook-mail-import-row">
      <strong>${escapeHtml(item.date || '-')}</strong>
      <span>新增 ${Number(item.api_added || 0)} · 更新 ${Number(item.api_updated || 0)}</span>
      <b>${Number(item.api_requests || 0)} 次</b>
    </div>`).join('')
}

function renderOutlookMails(payload) {
  state.outlookMails = payload || null
  state.outlookMailItems = Array.isArray(payload?.items) ? payload.items : []
  state.outlookMailPage = Number(payload?.page) || 1
  state.outlookMailPageSize = Number(payload?.page_size) || state.outlookMailPageSize
  state.outlookMailPages = Number(payload?.pages) || 1
  state.outlookMailTotal = Number(payload?.total) || 0
  state.outlookMailStatus = payload?.status || state.outlookMailStatus
  const summary = payload?.summary || {}
  const importStats = payload?.import_stats || {}
  const today = importStats.today || {}
  const recent = Array.isArray(importStats.recent) ? importStats.recent : []
  const week = recent.slice(0, 7).reduce((sum, item) => sum + Number(item.api_added || 0), 0)
  $('#sideOutlookMailCount').textContent = state.outlookMailTotal
  $('#outlookMailStatTotal').textContent = Number(summary.total ?? state.outlookMailTotal)
  $('#outlookMailStatToday').textContent = Number(today.api_added || 0)
  $('#outlookMailStatTodayMeta').textContent = `更新 ${Number(today.api_updated || 0)} · ${Number(today.api_requests || 0)} 次请求`
  $('#outlookMailStatWeek').textContent = week
  $('#outlookMailListMeta').textContent = state.outlookMailSearch || state.outlookMailStatus !== 'all'
    ? `${state.outlookMailTotal} 条匹配`
    : `${Number(summary.total || state.outlookMailTotal)} 个邮箱`
  renderOutlookMailImportStats(importStats)
  $$('[data-outlook-mail-status]').forEach((button) => {
    const active = button.dataset.outlookMailStatus === state.outlookMailStatus
    button.classList.toggle('active', active)
    button.setAttribute('aria-selected', active ? 'true' : 'false')
  })
  renderOutlookMailPagination()
  const body = $('#outlookMailBody')
  const empty = $('#outlookMailEmpty')
  body.innerHTML = ''
  empty.hidden = state.outlookMailItems.length > 0
  empty.querySelector('strong').textContent = state.outlookMailSearch || state.outlookMailStatus !== 'all' ? '没有匹配的 Outlook 邮箱' : '暂无 Outlook 邮箱'
  state.outlookMailItems.forEach((item) => {
    const [label, tone, icon] = outlookPoolStatusPresentation(item.status)
    const error = item.last_error || ''
    const row = document.createElement('tr')
    row.innerHTML = `
      <td class="email-cell" title="${escapeHtml(item.email || '')}"><span>${escapeHtml(item.email || '-')}</span></td>
      <td><span class="health-state ${tone}" title="${escapeHtml(item.last_error || label)}"><i class="ti ${icon}"></i>${escapeHtml(label)}</span></td>
      <td><time class="outlook-mail-time" title="${escapeHtml(item.imported_at || '')}">${escapeHtml(formatTime(item.imported_at))}</time></td>
      <td class="mail-note-cell"><span class="${error ? 'error' : ''}" title="${escapeHtml(error || '状态正常')}">${escapeHtml(error || '状态正常')}</span></td>`
    body.appendChild(row)
  })
}

async function loadOutlookMails(page = null) {
  const requestedPage = Math.max(1, Number(page ?? state.outlookMailPage) || 1)
  const params = new URLSearchParams({
    page: String(requestedPage),
    page_size: String(state.outlookMailPageSize),
    status: state.outlookMailStatus,
  })
  if (state.outlookMailSearch) params.set('query', state.outlookMailSearch)
  const payload = await api(`/api/outlook-mails?${params.toString()}`)
  renderOutlookMails(payload)
  return payload
}

async function importOutlookPool(source = 'settings') {
  const input = source === 'page' ? $('#outlookPoolPageInput') : $('#outlookPoolInput')
  const items = input.value.trim()
  if (!items) return toast('请粘贴 Outlook 邮箱池账号', 'error')
  const button = source === 'page' ? $('#outlookPoolPageImportButton') : $('#outlookPoolImportButton')
  setBusy(button, true, '导入中')
  try {
    const result = await api('/api/outlook-pool/import', {
      method: 'POST',
      body: JSON.stringify({ items }),
    })
    input.value = ''
    await loadOutlookPool(1)
    toast(`Outlook 邮箱池已导入：新增 ${result.added || 0}，更新 ${result.updated || 0}`)
  } finally {
    setBusy(button, false)
  }
}

async function deleteSelectedOutlookMailboxes() {
  const mailboxIds = [...state.outlookPoolSelected]
  if (!mailboxIds.length) return
  if (!window.confirm(`确定删除选中的 ${mailboxIds.length} 个 Outlook 基础邮箱？`)) return
  const button = $('#deleteSelectedOutlookButton')
  setBusy(button, true, '删除中')
  try {
    const result = await api('/api/outlook-pool', {
      method: 'DELETE',
      body: JSON.stringify({ mailbox_ids: mailboxIds, clear_all: false }),
    })
    state.outlookPoolSelected.clear()
    await loadOutlookPool(state.outlookPoolPage)
    toast(`已删除 ${result.removed || 0} 个 Outlook 邮箱`)
  } finally {
    setBusy(button, false)
    updateOutlookPoolSelectionUi()
  }
}

async function clearOutlookPool() {
  const total = Number(state.outlookPool?.summary?.total || 0)
  if (!total) return
  if (!window.confirm(`确定清空 Outlook 号池中的全部 ${total} 个基础邮箱？此操作会同时移除使用记录和异常项。`)) return
  const button = $('#clearOutlookPoolButton')
  setBusy(button, true, '清空中')
  try {
    const result = await api('/api/outlook-pool', {
      method: 'DELETE',
      body: JSON.stringify({ mailbox_ids: [], clear_all: true }),
    })
    state.outlookPoolSelected.clear()
    await loadOutlookPool(1)
    toast(`Outlook 号池已清空：删除 ${result.removed || 0} 个邮箱`)
  } finally {
    setBusy(button, false)
    updateOutlookPoolSelectionUi()
  }
}

async function deleteFailedOutlookMailboxes() {
  const failed = Number(state.outlookPool?.summary?.failed || 0)
  if (!failed) return
  if (!window.confirm(`确定删除全部 ${failed} 个异常 Outlook 邮箱？删除后不可从本地号池恢复。`)) return
  const button = $('#deleteFailedOutlookButton')
  setBusy(button, true, '删除中')
  try {
    const result = await api('/api/outlook-pool/failed', { method: 'DELETE' })
    state.outlookPoolSelected.clear()
    await loadOutlookPool(1)
    toast(`异常邮箱已删除：${result.removed || 0} 个`)
  } finally {
    setBusy(button, false)
    updateOutlookPoolSelectionUi()
  }
}

function renderAccounts(payload) {
  state.accounts = payload.items || []
  state.accountPage = Number(payload.page) || 1
  state.accountPageSize = Number(payload.page_size) || state.accountPageSize
  state.accountPages = Number(payload.pages) || 1
  state.accountTotal = Number(payload.total) || 0
  state.accountAllTotal = Number(payload.all_total) || 0
  state.accountCategory = payload.category || state.accountCategory
  const body = $('#accountsBody')
  const empty = $('#accountsEmpty')
  const filtered = state.accountSearch || state.accountCategory !== 'all'
  $('#accountCountLabel').textContent = filtered
    ? `${state.accountTotal} 条匹配 / ${state.accountAllTotal} 个账号`
    : `${state.accountAllTotal} 个账号`
  const sideAccountCount = $('#sideAccountCount')
  if (sideAccountCount) sideAccountCount.textContent = state.accountAllTotal
  const counts = payload.counts || {}
  $$('[data-account-category]').forEach((button) => {
    const category = button.dataset.accountCategory
    const active = category === state.accountCategory
    button.classList.toggle('active', active)
    button.setAttribute('aria-selected', active ? 'true' : 'false')
    const count = button.querySelector('b')
    if (count) count.textContent = Number(counts[category]) || 0
  })
  renderAccountPagination()
  body.innerHTML = ''
  empty.hidden = state.accounts.length > 0
  empty.querySelector('strong').textContent = filtered ? '没有匹配的账号' : '暂无账号'
  if (!state.accounts.length) return
  state.accounts.forEach((account) => {
    const [healthLabel, healthTone, healthIcon] = healthPresentation(account)
    const recovery = recoveryPresentation(account)
    const recoveryTime = account.health_recovery_updated_at || account.health_checked_at
    const recoveryHtml = recovery
      ? `<small class="health-recovery-line ${recovery[1]}" title="${escapeHtml(account.health_detail || recovery[0])}"><i class="ti ${recovery[2]}"></i><span>${escapeHtml(recovery[0])}</span>${recoveryTime ? `<time>${escapeHtml(formatTime(recoveryTime))}</time>` : ''}</small>`
      : ''
    const row = document.createElement('tr')
    row.innerHTML = `
      <td class="email-cell" title="${escapeHtml(account.email)}"><div class="account-identity"><span>${escapeHtml(account.email || '-')}</span><small class="provider-badge openai">ChatGPT</small></div></td>
      <td><div class="password-view"><span class="password-text">********</span><button class="password-toggle" type="button" data-password-id="${escapeHtml(account.id)}" title="显示密码" aria-label="显示密码"><i class="ti ti-eye"></i></button></div></td>
      <td><span class="rt-state ${account.has_refresh_token ? 'has' : 'missing'}"><i class="ti ${account.has_refresh_token ? 'ti-circle-check' : 'ti-circle-x'}"></i>${account.has_refresh_token ? '有 RT' : '无 RT'}</span></td>
      <td class="health-cell"><div><span class="health-state ${healthTone}" title="${escapeHtml(account.health_detail || healthLabel)}"><i class="ti ${healthIcon}"></i>${healthLabel}</span>${recoveryHtml}</div></td>
      <td class="token-cell" title="${escapeHtml(account.access_token)}">${escapeHtml(account.access_token || '-')}</td>
      <td class="survival-cell" title="本轮开始：${escapeHtml(formatTime(account.survival_started_at || account.created_at))}${account.survival_ended_at ? `；结束：${escapeHtml(formatTime(account.survival_ended_at))}` : ''}"><strong>${escapeHtml(formatLifetime(account.survival_seconds))}</strong><small>${account.survival_ended_at ? '本轮已结束' : `本轮存活${Number(account.survival_recovery_count) ? ` · 恢复 ${Number(account.survival_recovery_count)} 次` : ''}`}</small></td>
      <td><div class="row-actions"><button class="row-health" type="button" data-health-id="${escapeHtml(account.id)}" title="检测账号" aria-label="检测账号" ${healthTone === 'checking' ? 'disabled' : ''}><i class="ti ti-heartbeat"></i></button><button class="row-delete" type="button" data-delete-id="${escapeHtml(account.id)}" title="删除" aria-label="删除账号"><i class="ti ti-trash"></i></button></div></td>`
    body.appendChild(row)
  })
}

async function loadAccounts(page = null) {
  const requestedPage = Math.max(1, Number(page ?? state.accountTargetPage) || 1)
  if (page !== null) state.accountTargetPage = requestedPage
  if (state.accountsLoading) {
    state.accountReloadPending = true
    return null
  }
  state.accountsLoading = true
  try {
    const params = new URLSearchParams({
      page: String(requestedPage),
      page_size: String(state.accountPageSize),
      category: state.accountCategory,
    })
    if (state.accountSearch) params.set('query', state.accountSearch)
    const payload = await api(`/api/accounts?${params.toString()}`)
    renderAccounts(payload)
    return payload
  } finally {
    state.accountsLoading = false
    if (state.accountReloadPending || state.accountTargetPage !== state.accountPage) {
      state.accountReloadPending = false
      window.setTimeout(() => loadAccounts().catch((error) => toast(error.message, 'error')), 0)
    }
  }
}

async function deleteAccount(accountId) {
  if (!accountId || !window.confirm('确认删除这个本地账号？')) return
  const result = await api('/api/accounts', {
    method: 'DELETE',
    body: JSON.stringify({ account_ids: [accountId] }),
  })
  state.passwords.delete(accountId)
  toast(`已删除 ${result.removed || 0} 个账号`)
  await Promise.all([loadAccounts(), loadDashboard()])
}

async function togglePassword(button) {
  const accountId = button.dataset.passwordId
  const text = button.closest('.password-view').querySelector('.password-text')
  const showing = button.dataset.showing === 'true'
  if (showing) {
    text.textContent = '********'
    button.dataset.showing = 'false'
    button.title = '显示密码'
    button.setAttribute('aria-label', '显示密码')
    button.innerHTML = '<i class="ti ti-eye"></i>'
    return
  }
  if (!state.passwords.has(accountId)) {
    button.disabled = true
    try {
      const credentials = await api(`/api/accounts/${encodeURIComponent(accountId)}/credentials`)
      state.passwords.set(accountId, credentials.password || '')
    } finally {
      button.disabled = false
    }
  }
  text.textContent = state.passwords.get(accountId) || '-'
  button.dataset.showing = 'true'
  button.title = '隐藏密码'
  button.setAttribute('aria-label', '隐藏密码')
  button.innerHTML = '<i class="ti ti-eye-off"></i>'
}

async function startHealthCheck(accountIds = []) {
  const joining = state.health?.state === 'running'
  const result = await api('/api/accounts/health/check', {
    method: 'POST',
    body: JSON.stringify({ account_ids: accountIds }),
  })
  state.health = result
  await Promise.all([loadDashboard(), loadAccounts()])
  toast(joining ? '账号已加入当前并发检测批次' : accountIds.length ? '账号检测已启动' : '全部账号检测已启动')
}

async function stopHealthCheck() {
  const button = $('#healthStopButton')
  setBusy(button, true, '停止中')
  try {
    const result = await api('/api/accounts/health/stop', { method: 'POST' })
    state.health = result
    await loadDashboard()
    toast('检测/恢复停止请求已提交', 'warning')
  } finally {
    setBusy(button, false)
  }
}

async function saveRegistrationConcurrency(provider) {
  const resolved = resolveRegistrationProvider(provider)
  const input = registrationInputs(resolved).concurrency
  const concurrency = Number(input.value)
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 50) {
    const registration = providerRegistrationSettings(state.settings?.registration || {}, resolved)
    input.value = state.registrationDrafts[resolved]?.concurrency || registration.concurrency || 1
    return toast('注册并发需要在 1 到 50 之间', 'error')
  }
  const result = await api('/api/settings/registration/concurrency', {
    method: 'PUT',
    body: JSON.stringify({ concurrency, provider: resolved }),
  })
  state.settings = state.settings || {}
  state.settings.registration = state.settings.registration || {}
  state.settings.registration.concurrency = result.concurrency || concurrency
  state.settings.registration.providers = state.settings.registration.providers || {}
  state.settings.registration.providers[resolved] = {
    ...(state.settings.registration.providers[resolved] || {}),
    concurrency: result.concurrency || concurrency,
  }
  rememberRegistrationDraft(resolved)
  toast(`${registrationProviderName(resolved)} 注册并发已保存`)
}

async function startRegistration(provider, options = {}) {
  const resolved = resolveRegistrationProvider(provider)
  const force = Boolean(options.force)
  const inputs = registrationInputs(resolved)
  const count = Number(inputs.count.value)
  const concurrency = Number(inputs.concurrency.value)
  if (!Number.isInteger(count) || count < 1 || count > 100) return toast('注册数量需要在 1 到 100 之间', 'error')
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 50) return toast('并发数需要在 1 到 50 之间', 'error')
  if (jobIsActive(jobFor(resolved))) return toast(`${registrationProviderName(resolved)} 注册任务正在运行`, 'warning')
  const button = $(`#${resolved}${force ? 'Force' : 'Start'}Button`)
  state.pendingJobStarts.add(resolved)
  setBusy(button, true, '启动中')
  try {
    const job = await api('/api/registration/start', {
      method: 'POST',
      body: JSON.stringify({ count, concurrency, provider: resolved, channel: registrationChannel(), force }),
    })
    const dashboard = await api('/api/dashboard')
    if (!dashboard.jobs || typeof dashboard.jobs !== 'object') {
      dashboard.jobs = { ...(state.jobs || {}), [resolved]: job }
    }
    renderDashboard(dashboard)
    state.registrationDrafts[resolved] = { count, concurrency }
    if (job.state === 'skipped') toast(job.message || '云端容量充足，本轮已跳过', 'warning')
    else toast(force ? `${registrationProviderName(resolved)} 强制补号任务已启动` : `${registrationProviderName(resolved)} 注册任务已启动`)
  } finally {
    state.pendingJobStarts.delete(resolved)
    setBusy(button, false)
    renderProviderJob(resolved, jobFor(resolved))
  }
}

function forceRegistration(provider) {
  return startRegistration(provider, { force: true })
}

async function stopRegistration(provider) {
  const resolved = resolveRegistrationProvider(provider)
  const button = $(`#${resolved}StopButton`)
  state.pendingJobStops.add(resolved)
  setBusy(button, true, '停止中')
  try {
    const job = await api('/api/registration/stop', {
      method: 'POST',
      body: JSON.stringify({ provider: resolved }),
    })
    const dashboard = await api('/api/dashboard')
    if (!dashboard.jobs || typeof dashboard.jobs !== 'object') {
      dashboard.jobs = { ...(state.jobs || {}), [resolved]: job }
    }
    renderDashboard(dashboard)
    toast(`${registrationProviderName(resolved)} 停止请求已提交`, 'warning')
  } finally {
    state.pendingJobStops.delete(resolved)
    setBusy(button, false)
    renderProviderJob(resolved, jobFor(resolved))
  }
}

async function changePassword() {
  const currentPassword = $('#currentPassword').value
  const newPassword = $('#newPassword').value
  if (!currentPassword || newPassword.length < 8) return toast('请填写当前密码，新密码至少 8 位', 'error')
  const button = $('#changePasswordButton')
  setBusy(button, true, '更新中')
  try {
    await api('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    })
    toast('密码已更新，请重新登录')
    window.setTimeout(showLogin, 700)
  } finally {
    setBusy(button, false)
  }
}

async function toggleMonitor() {
  const button = $('#monitorButton')
  const enabled = Boolean(state.monitor?.enabled)
  setBusy(button, true, enabled ? '停止中' : '开启中')
  try {
    await api(enabled ? '/api/monitor/stop' : '/api/monitor/start', { method: 'POST' })
    await Promise.all([loadDashboard(), loadSettings(), loadLogs()])
    toast(enabled ? '自动监听已停止' : '自动监听已开启', enabled ? 'warning' : 'success')
  } finally {
    setBusy(button, false)
  }
}

$('#loginForm').addEventListener('submit', async (event) => {
  event.preventDefault()
  const button = $('#loginButton')
  const error = $('#loginError')
  error.hidden = true
  setBusy(button, true, '登录中')
  try {
    const payload = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username: $('#loginUsername').value.trim(), password: $('#loginPassword').value }),
    })
    state.logCursor = 0
    state.visibleLogCounts = { openai: 0 }
    await showApp(payload.username)
  } catch (failure) {
    error.textContent = failure.message
    error.hidden = false
  } finally {
    setBusy(button, false)
  }
})

$$('[data-page]').forEach((button) => button.addEventListener('click', () => setPage(button.dataset.page)))
$$('[data-nav]').forEach((button) => button.addEventListener('click', () => setPage(button.dataset.nav)))
$('#refreshButton').addEventListener('click', () => Promise.all([loadDashboard(), loadLogs()]).catch((error) => toast(error.message, 'error')))
$('#reloadAccountsButton').addEventListener('click', () => loadAccounts().catch((error) => toast(error.message, 'error')))
$('#healthCheckAllButton').addEventListener('click', () => startHealthCheck().catch((error) => toast(error.message, 'error')))
$('#healthStopButton').addEventListener('click', () => stopHealthCheck().catch((error) => toast(error.message, 'error')))
registrationProviders.forEach((provider) => {
  const inputs = registrationInputs(provider)
  inputs.count.addEventListener('input', () => rememberRegistrationDraft(provider))
  inputs.concurrency.addEventListener('input', () => rememberRegistrationDraft(provider))
  inputs.concurrency.addEventListener('change', () => saveRegistrationConcurrency(provider).catch((error) => toast(error.message, 'error')))
})
$$('input[name="registrationChannel"]').forEach((input) => input.addEventListener('change', () => {
  state.settings = state.settings || {}
  state.settings.registration = state.settings.registration || {}
  state.settings.registration.channel = registrationChannel()
  renderProviderJob('openai', jobFor('openai'))
}))
$$('[data-start-provider]').forEach((button) => button.addEventListener('click', () => startRegistration(button.dataset.startProvider).catch((error) => toast(error.message, 'error'))))
$$('[data-force-provider]').forEach((button) => button.addEventListener('click', () => forceRegistration(button.dataset.forceProvider).catch((error) => toast(error.message, 'error'))))
$$('[data-stop-provider]').forEach((button) => button.addEventListener('click', () => stopRegistration(button.dataset.stopProvider).catch((error) => toast(error.message, 'error'))))
$('#monitorButton').addEventListener('click', () => toggleMonitor().catch((error) => toast(error.message, 'error')))
$('#saveSettingsButton').addEventListener('click', () => saveSettings().catch((error) => toast(error.message, 'error')))
$('#outlookPoolImportButton').addEventListener('click', () => importOutlookPool().catch((error) => toast(error.message, 'error')))
$('#outlookPoolPageImportButton').addEventListener('click', () => importOutlookPool('page').catch((error) => toast(error.message, 'error')))
$('#reloadOutlookPoolButton').addEventListener('click', () => loadOutlookPool().catch((error) => toast(error.message, 'error')))
$('#reloadOutlookMailsButton').addEventListener('click', () => loadOutlookMails().catch((error) => toast(error.message, 'error')))
$('#exportOutlookMailsButton').addEventListener('click', () => { window.location.href = '/api/outlook-mails/export.txt?format=detail' })
$('#deleteSelectedOutlookButton').addEventListener('click', () => deleteSelectedOutlookMailboxes().catch((error) => toast(error.message, 'error')))
$('#clearOutlookPoolButton').addEventListener('click', () => clearOutlookPool().catch((error) => toast(error.message, 'error')))
$('#deleteFailedOutlookButton').addEventListener('click', () => deleteFailedOutlookMailboxes().catch((error) => toast(error.message, 'error')))
$('#changePasswordButton').addEventListener('click', () => changePassword().catch((error) => toast(error.message, 'error')))
$('#exportAccountsButton').addEventListener('click', () => { window.location.href = '/api/accounts/export' })
$('#accountsPrevPage').addEventListener('click', () => loadAccounts(state.accountPage - 1).catch((error) => toast(error.message, 'error')))
$('#accountsNextPage').addEventListener('click', () => loadAccounts(state.accountPage + 1).catch((error) => toast(error.message, 'error')))
$('#accountsPageNumbers').addEventListener('click', (event) => {
  const button = event.target.closest('[data-account-page]')
  if (!button || button.matches('[aria-current="page"]')) return
  loadAccounts(Number(button.dataset.accountPage)).catch((error) => toast(error.message, 'error'))
})
$('#accountsPageSize').addEventListener('change', (event) => {
  state.accountPageSize = Number(event.target.value) || 20
  state.accountPage = 1
  state.accountTargetPage = 1
  loadAccounts(1).catch((error) => toast(error.message, 'error'))
})
$('#outlookPoolPrevPage').addEventListener('click', () => loadOutlookPool(state.outlookPoolPage - 1).catch((error) => toast(error.message, 'error')))
$('#outlookPoolNextPage').addEventListener('click', () => loadOutlookPool(state.outlookPoolPage + 1).catch((error) => toast(error.message, 'error')))
$('#outlookPoolPageNumbers').addEventListener('click', (event) => {
  const button = event.target.closest('[data-outlook-page]')
  if (!button || button.matches('[aria-current="page"]')) return
  loadOutlookPool(Number(button.dataset.outlookPage)).catch((error) => toast(error.message, 'error'))
})
$('#outlookPoolPageSize').addEventListener('change', (event) => {
  state.outlookPoolPageSize = Number(event.target.value) || 20
  state.outlookPoolPage = 1
  loadOutlookPool(1).catch((error) => toast(error.message, 'error'))
})
$('#outlookPoolSelectPage').addEventListener('change', (event) => {
  state.outlookPoolItems.forEach((item) => {
    const id = String(item.id || '')
    if (!id) return
    if (event.target.checked) state.outlookPoolSelected.add(id)
    else state.outlookPoolSelected.delete(id)
  })
  updateOutlookPoolSelectionUi()
})
$('#outlookPoolBody').addEventListener('change', (event) => {
  const input = event.target.closest('[data-outlook-select-id]')
  if (!input) return
  if (input.checked) state.outlookPoolSelected.add(input.dataset.outlookSelectId)
  else state.outlookPoolSelected.delete(input.dataset.outlookSelectId)
  updateOutlookPoolSelectionUi()
})
$('#outlookPoolCategories').addEventListener('click', (event) => {
  const button = event.target.closest('[data-outlook-status]')
  if (!button || button.dataset.outlookStatus === state.outlookPoolStatus) return
  state.outlookPoolStatus = button.dataset.outlookStatus
  state.outlookPoolPage = 1
  loadOutlookPool(1).catch((error) => toast(error.message, 'error'))
})
$('#outlookPoolSearch').addEventListener('input', (event) => {
  state.outlookPoolSearch = event.target.value.trim()
  $('#outlookPoolSearchClear').hidden = !state.outlookPoolSearch
  state.outlookPoolPage = 1
  window.clearTimeout(state.outlookPoolSearchTimer)
  state.outlookPoolSearchTimer = window.setTimeout(() => {
    loadOutlookPool(1).catch((error) => toast(error.message, 'error'))
  }, 280)
})
$('#outlookPoolSearchClear').addEventListener('click', () => {
  window.clearTimeout(state.outlookPoolSearchTimer)
  $('#outlookPoolSearch').value = ''
  $('#outlookPoolSearchClear').hidden = true
  state.outlookPoolSearch = ''
  state.outlookPoolPage = 1
  loadOutlookPool(1).catch((error) => toast(error.message, 'error'))
})
$('#outlookMailPrevPage').addEventListener('click', () => loadOutlookMails(state.outlookMailPage - 1).catch((error) => toast(error.message, 'error')))
$('#outlookMailNextPage').addEventListener('click', () => loadOutlookMails(state.outlookMailPage + 1).catch((error) => toast(error.message, 'error')))
$('#outlookMailPageNumbers').addEventListener('click', (event) => {
  const button = event.target.closest('[data-outlook-mail-page]')
  if (!button || button.matches('[aria-current="page"]')) return
  loadOutlookMails(Number(button.dataset.outlookMailPage)).catch((error) => toast(error.message, 'error'))
})
$('#outlookMailPageSize').addEventListener('change', (event) => {
  state.outlookMailPageSize = Number(event.target.value) || 20
  state.outlookMailPage = 1
  loadOutlookMails(1).catch((error) => toast(error.message, 'error'))
})
$('#outlookMailCategories').addEventListener('click', (event) => {
  const button = event.target.closest('[data-outlook-mail-status]')
  if (!button || button.dataset.outlookMailStatus === state.outlookMailStatus) return
  state.outlookMailStatus = button.dataset.outlookMailStatus
  state.outlookMailPage = 1
  loadOutlookMails(1).catch((error) => toast(error.message, 'error'))
})
$('#outlookMailSearch').addEventListener('input', (event) => {
  state.outlookMailSearch = event.target.value.trim()
  $('#outlookMailSearchClear').hidden = !state.outlookMailSearch
  state.outlookMailPage = 1
  window.clearTimeout(state.outlookMailSearchTimer)
  state.outlookMailSearchTimer = window.setTimeout(() => loadOutlookMails(1).catch((error) => toast(error.message, 'error')), 280)
})
$('#outlookMailSearchClear').addEventListener('click', () => {
  window.clearTimeout(state.outlookMailSearchTimer)
  $('#outlookMailSearch').value = ''
  $('#outlookMailSearchClear').hidden = true
  state.outlookMailSearch = ''
  state.outlookMailPage = 1
  loadOutlookMails(1).catch((error) => toast(error.message, 'error'))
})
$('#toggleOutlookApiKey').addEventListener('click', (event) => {
  const input = $('#outlookImportApiKey')
  const button = event.currentTarget
  const showing = input.type === 'text'
  input.type = showing ? 'password' : 'text'
  button.title = showing ? '显示 API Key' : '隐藏 API Key'
  button.setAttribute('aria-label', button.title)
  button.innerHTML = `<i class="ti ${showing ? 'ti-eye' : 'ti-eye-off'}"></i>`
})
$('#copyOutlookApiKey').addEventListener('click', async () => {
  const key = $('#outlookImportApiKey').value
  if (!key) return toast('API Key 尚未加载', 'error')
  await navigator.clipboard.writeText(key)
  toast('Outlook 导入 API Key 已复制')
})
$('#accountCategories').addEventListener('click', (event) => {
  const button = event.target.closest('[data-account-category]')
  if (!button || button.dataset.accountCategory === state.accountCategory) return
  state.accountCategory = button.dataset.accountCategory
  state.accountPage = 1
  state.accountTargetPage = 1
  loadAccounts(1).catch((error) => toast(error.message, 'error'))
})
$('#accountsSearch').addEventListener('input', (event) => {
  state.accountSearch = event.target.value.trim()
  $('#accountsSearchClear').hidden = !state.accountSearch
  state.accountPage = 1
  state.accountTargetPage = 1
  window.clearTimeout(state.accountSearchTimer)
  state.accountSearchTimer = window.setTimeout(() => {
    loadAccounts(1).catch((error) => toast(error.message, 'error'))
  }, 280)
})
$('#accountsSearchClear').addEventListener('click', () => {
  window.clearTimeout(state.accountSearchTimer)
  $('#accountsSearch').value = ''
  $('#accountsSearchClear').hidden = true
  state.accountSearch = ''
  state.accountPage = 1
  state.accountTargetPage = 1
  loadAccounts(1).catch((error) => toast(error.message, 'error'))
})
$('#accountsBody').addEventListener('click', (event) => {
  const healthButton = event.target.closest('[data-health-id]')
  if (healthButton) {
    startHealthCheck([healthButton.dataset.healthId]).catch((error) => toast(error.message, 'error'))
    return
  }
  const passwordButton = event.target.closest('[data-password-id]')
  if (passwordButton) {
    togglePassword(passwordButton).catch((error) => toast(error.message, 'error'))
    return
  }
  const button = event.target.closest('[data-delete-id]')
  if (button) deleteAccount(button.dataset.deleteId).catch((error) => toast(error.message, 'error'))
})
$$('[data-clear-logs]').forEach((button) => button.addEventListener('click', () => {
  $('#openaiLogList').innerHTML = '<div class="log-empty"><i class="ti ti-terminal"></i><span>等待 ChatGPT 新日志</span></div>'
  state.visibleLogCounts.openai = 0
  $('#openaiLogCount').textContent = '0 条'
}))
$('#logoutButton').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' }).catch(() => null)
  showLogin()
})

window.addEventListener('hashchange', () => setPage(location.hash.slice(1)))
setPage(location.hash.slice(1) || 'register')
checkSession()
