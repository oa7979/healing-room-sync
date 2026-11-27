/**
 * 용산힐링룸 캘린더 웹훅  (GitHub Actions oa7979/healing-room-sync 전용)
 *
 * POST JSON
 *   { secret, action: 'upsert' | 'ping' | 'list', key, title,
 *     start_date: 'YYYY-MM-DD', end_date_exclusive: 'YYYY-MM-DD',
 *     color_id: '3' | '9' | ..., location, description }
 *
 * 규칙
 *   · secret 은 스크립트 속성 WEBHOOK_SECRET 과 비교한다(코드에 값 없음).
 *   · description 안의 [hrsync-key:KEY] 로 이미 만든 일정을 찾아 갱신한다(중복 생성 금지).
 *   · 같은 기간·같은 플랫폼([직방]/[삼삼엠투])의 수동 등록 일정이 있으면 새로 만들지 않고
 *     그 일정에 키 표식만 붙인다(사용자가 쓴 제목·색은 그대로 둔다).
 */
var PROP_SECRET = 'WEBHOOK_SECRET';
var PROP_MERGED = 'MERGED_EVENT_IDS';
var KEY_PREFIX = '[hrsync-key:';
var DAY_MS = 24 * 60 * 60 * 1000;
var MAX_DAYS = 400;
// Events.setColor 는 hex('#dbadff')를 받지 않는다 → EventColor enum 으로 변환
// (문서: PALE_BLUE "1" … MAUVE "3" … BLUE "9" … RED "11")
function eventColor_(id) {
  var map = {
    '1': CalendarApp.EventColor.PALE_BLUE, '2': CalendarApp.EventColor.PALE_GREEN,
    '3': CalendarApp.EventColor.MAUVE, '4': CalendarApp.EventColor.PALE_RED,
    '5': CalendarApp.EventColor.YELLOW, '6': CalendarApp.EventColor.ORANGE,
    '7': CalendarApp.EventColor.CYAN, '8': CalendarApp.EventColor.GRAY,
    '9': CalendarApp.EventColor.BLUE, '10': CalendarApp.EventColor.GREEN,
    '11': CalendarApp.EventColor.RED
  };
  return map[String(id)] || null;
}
var PLATFORM_TAGS = ['[직방]', '[삼삼엠투]'];

function doGet() {
  return json_({ ok: true, service: 'yongsan-healing-room-calendar-webhook' });
}

function doPost(e) {
  try {
    var body = {};
    if (e && e.postData && e.postData.contents) body = JSON.parse(e.postData.contents);
    var expected = PropertiesService.getScriptProperties().getProperty(PROP_SECRET);
    if (!expected) return json_({ ok: false, error: 'WEBHOOK_SECRET_NOT_SET' });
    if (!body.secret || String(body.secret) !== String(expected)) {
      return json_({ ok: false, error: 'unauthorized' });
    }
    var action = body.action || 'upsert';
    if (action === 'ping') return json_({ ok: true, action: 'ping', time: new Date().toISOString() });
    if (action === 'list') return list_(body);
    if (action === 'upsert') return upsert_(body);
    return json_({ ok: false, error: 'unknown action: ' + action });
  } catch (err) {
    return json_({ ok: false, error: String((err && err.stack) || err) });
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function day_(iso) {
  var s = String(iso || '');
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) throw new Error('bad date: ' + s);
  var p = s.split('-');
  var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
  if (iso_(d) !== s) throw new Error('invalid date: ' + s);
  return d;
}

function iso_(d) {
  return Utilities.formatDate(d, 'Asia/Seoul', 'yyyy-MM-dd');
}

function keyTag_(key) {
  return KEY_PREFIX + key + ']';
}

function cal_() {
  return CalendarApp.getDefaultCalendar();
}

function withKey_(desc, key) {
  var body = String(desc || '');
  var tag = keyTag_(key);
  if (body.indexOf(tag) >= 0) return body;
  return body ? body + '\n' + tag : tag;
}

// 같은 플랫폼(zb/m33)의 예전 키 표식을 모두 지우고 새 키 표식만 남긴다.
function replaceKeys_(desc, family, tag) {
  var re = new RegExp('\\[hrsync-key:' + family + ':[^\\]]*\\]', 'g');
  var body = String(desc || '').replace(re, '').replace(/[ \t]+$/gm, '').trim();
  return body ? body + '\n' + tag : tag;
}

function platformTag_(title) {
  var t = String(title || '');
  for (var i = 0; i < PLATFORM_TAGS.length; i++) {
    if (t.indexOf(PLATFORM_TAGS[i]) === 0) return PLATFORM_TAGS[i];
  }
  return '';
}

function mergedIds_() {
  var raw = PropertiesService.getScriptProperties().getProperty(PROP_MERGED) || '';
  return raw ? raw.split(',') : [];
}

function markMerged_(id) {
  var ids = mergedIds_();
  if (ids.indexOf(id) < 0) {
    ids.push(id);
    PropertiesService.getScriptProperties().setProperty(PROP_MERGED, ids.join(','));
  }
}

function snapshot_(ev) {
  return {
    event_id: ev.getId(),
    title: ev.getTitle(),
    start: iso_(ev.getStartTime()),
    end: iso_(ev.getEndTime()),
    all_day: ev.isAllDayEvent(),
    color: ev.getColor()
  };
}

function list_(body) {
  var start = day_(body.start_date);
  var end = day_(body.end_date_exclusive);
  var events = cal_().getEvents(new Date(start.getTime() - DAY_MS), new Date(end.getTime() + DAY_MS));
  var out = [];
  for (var i = 0; i < events.length; i++) {
    var s = snapshot_(events[i]);
    s.description = events[i].getDescription();
    out.push(s);
  }
  return json_({ ok: true, action: 'list', count: out.length, events: out });
}

function upsert_(body) {
  var key = String(body.key || '').trim();
  var title = String(body.title || '').trim();
  if (!key) return json_({ ok: false, error: 'key required' });
  if (!title) return json_({ ok: false, error: 'title required' });
  var startIso = String(body.start_date || '');
  var endIso = String(body.end_date_exclusive || '');
  var start = day_(startIso);
  var end = day_(endIso);
  if (end.getTime() <= start.getTime()) return json_({ ok: false, error: 'end_date_exclusive must be after start_date' });
  if ((end.getTime() - start.getTime()) / DAY_MS > MAX_DAYS) return json_({ ok: false, error: 'period too long' });
  var colorId = body.color_id ? String(body.color_id) : '';
  var evColor = eventColor_(colorId);
  var location = String(body.location || '');
  var description = withKey_(body.description, key);
  var tag = keyTag_(key);
  var calendar = cal_();
  var events = calendar.getEvents(new Date(start.getTime() - DAY_MS), new Date(end.getTime() + DAY_MS));

  // 1) 키 표식이 붙은 일정 = 이 웹훅이 관리하는 일정
  var owned = null;
  for (var i = 0; i < events.length; i++) {
    if (String(events[i].getDescription() || '').indexOf(tag) >= 0) { owned = events[i]; break; }
  }
  if (owned) {
    var id = owned.getId();
    if (mergedIds_().indexOf(id) >= 0) {
      var extended = false;
      if (iso_(owned.getEndTime()) < endIso) {
        owned.setAllDayDates(owned.getStartTime(), end);
        extended = true;
      }
      var kept = snapshot_(owned);
      kept.action = 'kept-manual';
      kept.extended = extended;
      kept.has_key = true;
      return json_({ ok: true, key: key, result: kept });
    }
    owned.setTitle(title);
    owned.setAllDayDates(start, end);
    if (evColor) owned.setColor(evColor);
    if (location) owned.setLocation(location);
    owned.setDescription(description);
    var upd = snapshot_(owned);
    upd.action = 'updated';
    upd.has_key = true;
    return json_({ ok: true, key: key, result: upd });
  }

  // 1b) 같은 플랫폼(zb/m33)의 우리 일정이 기간만 바뀐 채 남아 있으면 새로 만들지 않고 키만 갱신
  var family = String(key).split(':')[0];
  var sibling = null;
  for (var k = 0; k < events.length; k++) {
    var cand = events[k];
    if (!cand.isAllDayEvent()) continue;
    var cdesc = String(cand.getDescription() || '');
    if (cdesc.indexOf('[hrsync-key:' + family + ':') < 0) continue;
    if (cdesc.indexOf(tag) >= 0) continue;
    sibling = cand;
    break;
  }
  if (sibling) {
    var sMerged = mergedIds_().indexOf(sibling.getId()) >= 0;
    if (sMerged) {
      if (iso_(sibling.getEndTime()) < endIso) sibling.setAllDayDates(sibling.getStartTime(), end);
      sibling.setDescription(replaceKeys_(sibling.getDescription(), family, tag));
    } else {
      sibling.setTitle(title);
      sibling.setAllDayDates(start, end);
      if (evColor) sibling.setColor(evColor);
      if (location) sibling.setLocation(location);
      sibling.setDescription(description);
    }
    var sib = snapshot_(sibling);
    sib.action = 'rekeyed';
    sib.has_key = true;
    return json_({ ok: true, key: key, result: sib });
  }

  // 2) 수동 등록된 같은 플랫폼 일정이 있으면 중복 생성하지 않고 키만 병합
  var platform = platformTag_(title);
  var manual = null;
  var exact = null;
  for (var j = 0; j < events.length; j++) {
    var ev = events[j];
    if (!ev.isAllDayEvent()) continue;
    if (platform && String(ev.getTitle() || '').indexOf(platform) < 0) continue;
    if (!manual) manual = ev;
    if (iso_(ev.getStartTime()) === startIso && iso_(ev.getEndTime()) === endIso) { exact = ev; break; }
  }
  if (exact || manual) {
    var target = exact || manual;
    var extended2 = false;
    if (iso_(target.getEndTime()) < endIso) {
      target.setAllDayDates(target.getStartTime(), end);
      extended2 = true;
    }
    if (String(target.getDescription() || '').indexOf(tag) < 0) {
      target.setDescription(withKey_(target.getDescription(), key));
    }
    markMerged_(target.getId());
    var merged = snapshot_(target);
    merged.action = exact ? 'merged-existing' : 'merged-overlap';
    merged.extended = extended2;
    merged.has_key = true;
    return json_({ ok: true, key: key, result: merged });
  }

  // 3) 새로 만든다
  var created = calendar.createAllDayEvent(title, start, end, {
    location: location,
    description: description
  });
  if (evColor) created.setColor(evColor);
  var made = snapshot_(created);
  made.action = 'created';
  made.has_key = true;
  return json_({ ok: true, key: key, result: made });
}
