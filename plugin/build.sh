#!/usr/bin/env bash
# Builds buildforge.hpi (the Build Forge plugin) against YOUR Jenkins, with only a JDK (no Maven, no internet).
#   ./build.sh                      # uses /usr/share/java/jenkins.war or /usr/share/jenkins/jenkins.war
#   ./build.sh /path/to/jenkins.war
# Output: ./buildforge.hpi  (upload it in Manage Jenkins > Plugins > Advanced settings > Deploy Plugin)
set -euo pipefail
cd "$(dirname "$0")"
VERSION=2.2.1
WAR="${1:-}"
if [ -z "$WAR" ]; then
  for c in /usr/share/java/jenkins.war /usr/share/jenkins/jenkins.war /usr/lib/jenkins/jenkins.war; do
    [ -f "$c" ] && WAR="$c" && break
  done
fi
[ -f "${WAR:-}" ] || { echo "jenkins.war not found; pass its path: ./build.sh /path/to/jenkins.war" >&2; exit 1; }
command -v javac >/dev/null || { echo "javac not found: install a JDK at least as new as the Java Jenkins runs on (e.g. apt install openjdk-21-jdk-headless)" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
unzip -q -o "$WAR" 'WEB-INF/lib/*.jar' -d "$WORK/war"
# The servlet API comes with winstone.jar (at the root of older wars, under executable/ on newer ones).
unzip -q -o "$WAR" 'winstone.jar' 'executable/winstone.jar' -d "$WORK/war" 2>/dev/null || true
CORE_VERSION="$(unzip -p "$WAR" META-INF/MANIFEST.MF | sed -n 's/^Jenkins-Version: *//p' | tr -d '\r')"
CP="$WORK/war/WEB-INF/lib/*:$WORK/war/winstone.jar:$WORK/war/executable/winstone.jar"
mkdir -p "$WORK/classes" "$WORK/hpi/WEB-INF/lib" "$WORK/hpi/META-INF"

# Java 11 bytecode loads on every Jenkins from 2.346 on. Newer cores are built for a newer Java (the class file version in the
# error: 61 = Java 17, 65 = Java 21), and javac can only read classes up to its own version, so those need a matching JDK.
compile() { javac -nowarn --release "$1" -encoding UTF-8 -cp "$CP" -d "$WORK/classes" $(find src/main/java -name '*.java') 2>"$WORK/javac.log"; }
if ! compile 11; then
  NEED="$(sed -n 's/.*class file has wrong version \([0-9]*\)\..*/\1/p' "$WORK/javac.log" | sort -n | tail -1)"
  if [ -n "$NEED" ]; then
    NEED_JDK=$((NEED - 44)); HAVE_JDK="$(javac -version 2>&1 | sed -n 's/^javac \([0-9]*\).*/\1/p')"
    if [ -n "$HAVE_JDK" ] && [ "$HAVE_JDK" -lt "$NEED_JDK" ]; then
      echo "This Jenkins ($CORE_VERSION) is built for Java $NEED_JDK but javac here is Java $HAVE_JDK." >&2
      echo "Install a JDK $NEED_JDK or newer (e.g. sudo apt install openjdk-$NEED_JDK-jdk-headless) and run this again" >&2
      echo "(if several JDKs are installed: JAVA_HOME=/usr/lib/jvm/<jdk-$NEED_JDK> PATH=\$JAVA_HOME/bin:\$PATH bash build.sh ...)." >&2
      exit 1
    fi
    rm -rf "$WORK/classes"; mkdir -p "$WORK/classes"
    compile "$NEED_JDK" || { cat "$WORK/javac.log" >&2; exit 1; }
  else
    cat "$WORK/javac.log" >&2; exit 1
  fi
fi
[ -f "$WORK/classes/META-INF/annotations/hudson.Extension" ] || { echo "extension index missing (sezpoz did not run)" >&2; exit 1; }
cp -r src/main/resources/. "$WORK/classes/"
# Jenkins only (de)serialises classes from jars whose own manifest says they belong to a plugin (JEP-200),
# so the inner jar carries the same manifest as the .hpi.
cat > "$WORK/hpi/META-INF/MANIFEST.MF" <<MF
Manifest-Version: 1.0
Short-Name: buildforge
Long-Name: Build Forge
Extension-Name: buildforge
Plugin-Version: $VERSION
Implementation-Version: $VERSION
Jenkins-Version: 2.346
Hudson-Version: 2.346
Group-Id: io.jenkins.plugins
Url: https://buildforge.local/
Built-Against: $CORE_VERSION
MF
(cd "$WORK/classes" && jar cfm "$WORK/hpi/WEB-INF/lib/buildforge.jar" "$WORK/hpi/META-INF/MANIFEST.MF" .)

cp -r src/main/webapp/. "$WORK/hpi/"

rm -f buildforge.hpi
(cd "$WORK/hpi" && jar cfm "$OLDPWD/buildforge.hpi" META-INF/MANIFEST.MF $(ls -A | grep -v '^META-INF$'))
echo "Built $(pwd)/buildforge.hpi (plugin $VERSION, compiled against Jenkins $CORE_VERSION)"
