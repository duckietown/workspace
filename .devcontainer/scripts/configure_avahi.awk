# Idempotently rewrite avahi-daemon.conf for the dev container startup path.
# Existing ordering and unrelated settings are preserved while the managed
# server/reflector keys are normalized and forced to the requested values.

# Keep track of output so appended sections can be separated cleanly.
function emit(line) {
    print line
    last_output = line
    printed_any = 1
}

# Treat commented-out key/value settings as candidates for replacement. This
# avoids leaving stale examples next to the active setting we need to enforce.
function normalize_key_value(line,    pos, prefix_key, value, key, prefix) {
    pos = index(line, "=")
    if (pos == 0) {
        return line
    }

    prefix_key = substr(line, 1, pos - 1)
    value = substr(line, pos + 1)

    key = prefix_key
    sub(/[[:space:]]*$/, "", key)
    if (key !~ /^[[:space:]]*#?[[:space:]]*[A-Za-z0-9][A-Za-z0-9_-]*$/) {
        return line
    }

    prefix = prefix_key
    sub(/[A-Za-z0-9][A-Za-z0-9_-]*[[:space:]]*$/, "", prefix)
    sub(/^[[:space:]]*#?[[:space:]]*/, "", key)
    return prefix key "=" value
}

# Called when leaving a section; appends the managed key if it was absent.
function ensure_target(section,    key, value, pair) {
    if (section == "server") {
        key = "allow-interfaces"
        value = iface
    } else if (section == "reflector") {
        key = "enable-reflector"
        value = "no"
    } else {
        return
    }

    pair = section SUBSEP key
    if (!(pair in seen_target)) {
        emit(key "=" value)
        seen_target[pair] = 1
    }
}

# Add a whole missing section at EOF, preserving a readable blank-line spacer.
function append_missing_section(section, key, value) {
    if (printed_any && last_output != "") {
        emit("")
    }
    emit("[" section "]")
    emit(key "=" value)
}

# Emit each input line unless it is a managed setting that should be replaced.
{
    line = normalize_key_value($0)
    stripped = line
    sub(/^[[:space:]]+/, "", stripped)
    sub(/[[:space:]]+$/, "", stripped)

    if (stripped ~ /^\[[^]]+\]$/) {
        # Finish the previous section before recording the new section header.
        if (current_section != "") {
            ensure_target(current_section)
        }

        current_section = stripped
        sub(/^\[/, "", current_section)
        sub(/\]$/, "", current_section)
        seen_section[current_section] = 1
        emit(line)
        next
    }

    handled = 0
    if (current_section == "server" || current_section == "reflector") {
        pos = index(line, "=")
        if (pos > 0) {
            key = substr(line, 1, pos - 1)
            sub(/[[:space:]]*$/, "", key)
            sub(/^[[:space:]]*#?[[:space:]]*/, "", key)

            if (current_section == "server") {
                target_key = "allow-interfaces"
                target_value = iface
            } else {
                target_key = "enable-reflector"
                target_value = "no"
            }

            pair = current_section SUBSEP target_key
            if (key == target_key) {
                # Replace the first managed key and suppress later duplicates.
                if (!(pair in seen_target)) {
                    emit(target_key "=" target_value)
                    seen_target[pair] = 1
                }
                handled = 1
            }
        }
    }

    if (!handled) {
        emit(line)
    }
}

END {
    # Finish the trailing section and add required sections that never appeared.
    if (current_section != "") {
        ensure_target(current_section)
    }

    if (!("server" in seen_section)) {
        append_missing_section("server", "allow-interfaces", iface)
    }
    if (!("reflector" in seen_section)) {
        append_missing_section("reflector", "enable-reflector", "no")
    }
}
