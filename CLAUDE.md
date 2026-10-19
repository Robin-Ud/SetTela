# SetTela

GUI GTK4 (PyGObject) pra monitores do Hyprland. Complementa a função `settela` do `~/.bashrc`
— não criar outro script de espelhamento.

- Tudo via `hyprctl --batch "keyword monitor ..."`; nunca escrever na config do Hyprland.
  Persistência é o `pessoal/lembrados.json` (fora do git) (chave = descrições do conjunto, ver `chave`/`conjunto`),
  gravado no "Manter" e reaplicado pelo `--vigiar` (socket2: monitoradded/removed/configreloaded).
- O vigia só reaplica quando o CONJUNTO muda (ou após reload) — ligar/desligar pela janela não
  muda o conjunto, então os dois não brigam. Trava em `$XDG_RUNTIME_DIR/settela-vigia.lock`.
- `comandos_centralizar`: recentraliza as janelas `rud.settela` após mudar monitores
  (desconta `reserved` da waybar; principal volta a `TAMANHO_JANELA` quando cabe).
- Lógica pura (`ler_monitores`, `regra`, `regras_em_ordem`, `validar`, `grudar`, `encaixar`,
  `normalizar`, `preset`, `lembrar`, `recordar`, `comandos_centralizar`) fica sem import de `gi` — `gi` só entra dentro de `main()`.
  Mudou lógica → adicionar caso em `testes()` e rodar `./settela_test.sh`.
- `regras_em_ordem`: liga normais → espelhos → desliga, pra nunca ficar sem tela.
- Coordenadas são lógicas (pixels ÷ escala); `mirrorOf` do hyprctl vem como id.
- App id `rud.settela` (instância única; precisa de windowrule float no Hyprland).
- Teste real sem cabo: `hyprctl output create headless` / `hyprctl output remove HEADLESS-N`.
- Confirmação "Manter?" é uma barra DENTRO da janela principal (não diálogo): um Gtk.Window
  separado abria no meio da troca de monitores e o Hyprland o mandava pra workspace invisível
  ("Só externo" revertia sozinho sem o usuário ver). `centralizar_janelas` ainda faz `focuswindow`.

@pessoal/CLAUDE.md
