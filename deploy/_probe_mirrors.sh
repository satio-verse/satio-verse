cd ~
for u in \
  "https://ghfast.top/https://github.com/satio-verse/satio-verse.git" \
  "https://gh-proxy.com/https://github.com/satio-verse/satio-verse.git" \
  "https://ghproxy.net/https://github.com/satio-verse/satio-verse.git" \
  "https://gh.llkk.cc/https://github.com/satio-verse/satio-verse.git" \
  "https://github.moeyy.xyz/https://github.com/satio-verse/satio-verse.git" ; do
  echo "== $u"
  git ls-remote --heads "$u" 2>&1 | head -3
done
