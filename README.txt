Audio Atelier
動画の音声切り出し・音声合成ツール
========================================

バージョン: v1.0
最終更新日: 2026年8月14日


■ はじめに

Audio Atelierは、動画から必要な音声を切り出したり、複数の音声を
並べたり重ねたりできるWindows用ソフトです。

最初に「必要なもの」と「起動方法」をお読みください。
操作に迷ったときは、目的に合う項目だけを参照できます。


■ 必要なもの

・Windows 10／11
・AudioAtelier.exe
・ffmpeg.exe
・ffprobe.exe
・ffplay.exe

FFmpeg本体は、この配布データには含まれていません。

3つのファイルは、次の機能で使います。

  ffmpeg.exe:
    波形の作成、音声の切り出し、合成、WAV・MP3・M4Aへの書き出し

  ffprobe.exe:
    動画・音声ファイルの再生時間の取得

  ffplay.exe:
    選択範囲と合成結果の試聴

読み込み・編集・保存にはffmpeg.exeとffprobe.exeが必要です。
ffplay.exeがなくても試聴以外の機能は使えますが、Audio Atelierの全機能を
利用する場合は、3ファイルをすべて用意してください。


■ FFmpegを入手する

FFmpeg公式ダウンロードページ:
  https://ffmpeg.org/download.html

FFmpeg公式サイトは、Windows用の実行ファイルを直接配布していません。
上記ページの「Windows EXE Files」から、案内されているWindowsビルドの
配布元を開いてください。

初めて導入する場合は、gyan.devの「release builds」にあるZIP形式の
「ffmpeg-release-essentials.zip」が扱いやすい構成です。Essentials Buildで
Audio Atelierに必要な機能を利用でき、binフォルダにはffmpeg.exe、
ffprobe.exe、ffplay.exeの3ファイルが入っています。

ダウンロード後はZIPを右クリックし、「すべて展開」で解凍してください。
ZIPを開いただけの状態ではなく、必ず通常のフォルダへ展開してから使います。


■ 方法A: Audio Atelierの横へ手動配置する（おすすめ）

Windowsの設定を変更しない、もっとも簡単な方法です。

1. ダウンロードしたFFmpegのZIPを展開します。
2. 展開してできた、binフォルダを含む最上位フォルダを確認します。
3. その最上位フォルダの名前を「ffmpeg」に変更します。
4. ffmpegフォルダをAudioAtelier.exeと同じ場所へ移動します。
   最終的に、次の配置になっていれば完了です。

  AudioAtelier.exe
  README.txt
  ffmpeg\
    bin\
      ffmpeg.exe
      ffprobe.exe
      ffplay.exe

5. 起動中のAudio Atelierを完全に終了し、AudioAtelier.exeを開き直します。

binフォルダ内のDLLなどは、削除せず一緒に置いておくことをおすすめします。
FFmpegを更新するときは、Audio Atelierを終了してからffmpegフォルダを
新しいものへ入れ替えてください。


■ 方法B: wingetでインストールする（PATH利用なら最短）

ほかのソフトやコマンドプロンプトからもFFmpegを使いたい場合の方法です。
Windows 10／11でwingetが利用できる環境なら、PATHを手作業で編集するより
簡単です。

1. スタートボタンを右クリックし、「ターミナル」または
   「Windows PowerShell」を開きます。

2. 次のコマンドを貼り付けて、Enterキーを押します。

  winget install --id Gyan.FFmpeg.Essentials -e

3. 使用条件の確認が表示された場合は、内容を確認して同意します。

4. インストール完了後、Audio AtelierとPowerShellを完全に終了します。

5. AudioAtelier.exeを開き直します。通常、Windowsの再起動は不要です。

Essentials BuildでAudio Atelierに必要な機能を利用できます。
Full Buildを使いたい場合は、次のコマンドでも構いません。

  winget install --id Gyan.FFmpeg -e

導入を確認する場合は、新しくPowerShellまたはコマンドプロンプトを開き、
次を1行ずつ実行します。

  where.exe ffmpeg
  where.exe ffprobe
  where.exe ffplay

3つとも保存場所が表示されれば完了です。PowerShellでは「where」だけだと
別の機能として扱われるため、「where.exe」と入力してください。

wingetコマンドが見つからない場合は、Microsoft Storeで「アプリ インストーラー」
を更新するか、方法Aの手動配置を利用してください。


■ 方法C: PATHを手作業で設定する

wingetを使わず、すでに展開したFFmpegへPATHを通す方法です。

1. 展開したFFmpegフォルダを、移動しない場所へ置きます。
   例: C:\Tools\ffmpeg

2. C:\Tools\ffmpeg\binの中に、ffmpeg.exe、ffprobe.exe、ffplay.exeが
   あることを確認します。

3. Windowsキーを押し、「環境変数」と検索します。

4. 「システム環境変数の編集」を開き、「環境変数」を選びます。

5. 上側の「ユーザー環境変数」にあるPathを選び、「編集」を押します。
   Pathにすでに登録されている項目は削除しないでください。

6. 「新規」を押し、次のフォルダを追加します。
   C:\Tools\ffmpeg\bin

7. 開いている画面を「OK」で閉じます。

8. 起動中のAudio Atelierとコマンドプロンプトをすべて終了し、開き直します。

確認する場合は、新しくPowerShellまたはコマンドプロンプトを開いて
次を1行ずつ実行します。

  where.exe ffmpeg
  where.exe ffprobe
  where.exe ffplay

3つとも保存場所が表示されれば、PATHの設定は完了です。


■ Audio AtelierはFFmpegを5か所から検索します

ffmpeg.exe、ffprobe.exe、ffplay.exeの3ファイルを、次のいずれかへ
置いてください。本ソフトは上から順番に検索します。

  (1) AudioAtelier.exeと同じフォルダ
  (2) その中のbinフォルダ
  (3) その中のffmpegフォルダ
  (4) その中のffmpeg\binフォルダ
  (5) WindowsのPATH

配置例:

  AudioAtelier.exe
  ffmpeg\
    bin\
      ffmpeg.exe
      ffprobe.exe
      ffplay.exe

ffmpeg.exeまたはffprobe.exeが見つからない場合は、読み込み・書き出しが
利用できないことを起動時に警告します。ffplay.exeだけが見つからない場合は、
試聴機能のみ利用できないことをお知らせします。


■ Audio Atelierを起動する

AudioAtelier.exeをダブルクリックします。インストールは不要です。

Windowsの保護画面が表示された場合は、発行元を確認したうえで
「詳細情報」から実行してください。本ソフトにはコード署名がありません。

初回起動時は、単一EXEの内部ファイルを一時的に展開するため、
画面が表示されるまで少し時間がかかる場合があります。

FFmpegを配置した直後やPATHを変更した直後は、起動中のAudio Atelierを
いったん完全に終了し、AudioAtelier.exeを開き直してください。
通常、Windows自体の再起動は必要ありません。


■ 動画から必要な音声だけを保存する

1. 「動画から音声を切り出す」タブを開きます。
2. 「動画・音声を選択」からファイルを読み込みます。
3. 波形上の青とオレンジのハンドルを動かし、残す範囲を決めます。
4. 「選択範囲を試聴」で内容を確認します。
5. WAV、MP3、M4Aから形式を選び、「選択範囲を書き出す」で保存します。

開始・終了位置は秒数でも入力できます。再生中は赤いバーが動き、
現在位置を示します。


■ 複数の音声を並べる・重ねる

1. 「音声を合成する」タブを開きます。
2. 「音声を追加」から、使用するファイルを選びます。
3. タイムライン上のクリップをドラッグして配置します。
4. 「全体を試聴」で合成結果を確認します。
5. WAV、MP3、M4Aから形式を選び、「合成音声を書き出す」で保存します。

クリップを左右へ動かすと開始時間、上下へ動かすとレーンが変わります。
時間が重なるクリップは同時に再生され、クリップ間の隙間は無音になります。

「全て順番に並べる」では、指定した無音時間を挟んで連結できます。
「全て先頭で重ねる」では、すべての音声が0秒から再生されます。


■ 対応形式

出力形式:
  WAV、MP3、M4A

主な動画入力形式:
  MP4、MOV、MKV、AVI、WebM、M4V、WMV、MPEG、MPG

主な音声入力形式:
  WAV、MP3、M4A、AAC、FLAC、OGG、WMA、Opus

上記はファイル選択画面に表示される主な形式です。「すべて」を選べば、
一覧にないファイルも指定できます。実際に読み込める形式は、お使いの
FFmpegが対応している形式に準じます。


■ app_dataフォルダについて

AudioAtelier.exeを起動すると、同じ場所にapp_dataフォルダが作成されます。
試聴用の一時ファイルを保存するためのフォルダです。

アプリを終了した状態であれば削除できます。必要になったときは
次回起動時に再作成されます。


■ 困ったときは

「FFmpegが見つからない」と表示される:
  ffmpeg.exe、ffprobe.exe、ffplay.exeの3ファイルが、上記5か所の
  いずれかにあるか確認してください。ffmpeg.exeとffprobe.exeは
  読み込み・書き出しに必要で、ffplay.exeは試聴に必要です。

FFmpegを配置したのに警告が消えない:
  Audio Atelierを閉じ、タスクマネージャーにAudioAtelier.exeが残って
  いないことを確認してから起動し直してください。PATHを使う場合は、
  新しく開いたコマンドプロンプトでwhereコマンドによる確認も行います。

  それでも新しいPATHが認識されない場合は、Windowsから一度サインアウトして
  サインインし直します。改善しないときに限り、Windowsを再起動してください。

AudioAtelier.exeを開いても画面が出ない:
  初回は数秒待ってください。配布ZIPの中から直接起動せず、「すべて展開」で
  解凍してから開きます。Program Filesなど書き込みが制限された場所は避け、
  デスクトップやドキュメントなど、自分で書き込める場所へ移してください。

  Windowsセキュリティやセキュリティソフトがブロックしていないかも確認します。
  セキュリティ機能そのものを無効にする必要はありません。ファイルが破損して
  いる可能性がある場合は、BOOTHの商品ページからもう一度ダウンロードします。

「WindowsによってPCが保護されました」と表示される:
  本ソフトにはコード署名がないため、SmartScreenの確認画面が表示される場合が
  あります。ダウンロード元が正しいことを確認し、「詳細情報」を開いてから
  「実行」を選びます。不審な場所から入手したファイルは実行しないでください。

音声を読み込めない、または保存できない:
  ファイルに音声トラックが含まれているか、保存先へ書き込む権限が
  あるかを確認してください。別の保存先や出力形式もお試しください。

再生できない:
  ffplay.exeの配置を確認してください。切り出しと書き出しには
  ffmpeg.exeとffprobe.exe、試聴にはffplay.exeを使用します。

解決しない場合は、ダウンロードしたBOOTHの商品ページからご連絡ください。
その際は、発生した操作、表示されたメッセージ、Windowsのバージョンを
添えていただけると確認しやすくなります。


■ ご利用上の注意

本ソフトは個人制作の無料ツールです。大切な動画・音声ファイルは、
あらかじめバックアップを取ってからご利用ください。

すべての環境やファイル形式での動作を保証するものではありません。
本ソフトの利用によって生じた損害について、作者は責任を負いかねます。

FFmpegは本ソフトとは別のソフトウェアです。利用条件やライセンスは、
入手したFFmpeg配布元の案内をご確認ください。
