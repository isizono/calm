"""ループバック(127.0.0.1)宛てHTTP呼び出し用の、プロキシを無視するurllibオープナー。

既定のurlopenはOS設定のプロキシを経由しうる。手動プロキシが設定され、かつ
その除外リストにIPアドレス表記の127.0.0.1が無い環境(Windowsのレジストリ設定を
含む)では、ループバック接続すら社内プロキシに送られてしまう。ここで作る接続は
すべてローカルの子プロセスへの接続なので、プロキシを常に無視する。
"""
import urllib.request

NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
