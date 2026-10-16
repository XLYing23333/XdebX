" ============ Basic ============
set nocompatible
set encoding=utf-8
set fileencoding=utf-8
set fileencodings=utf-8,gbk,gb2312,gb18030,big5
set termencoding=utf-8

" ============ UI ============
syntax on
set number
set relativenumber
set cursorline

" Keep 3 lines / 5 columns of context around the cursor
set scrolloff=3
set sidescrolloff=5

set mouse=a

" ============ Search ============
" smartcase: ignorecase unless the pattern contains uppercase
set hlsearch
set incsearch
set ignorecase
set smartcase

" ============ Indent / wrap ============
set tabstop=4
set shiftwidth=4
set expandtab
set smartindent

set wrap
set linebreak
set breakindent
set whichwrap+=<,>,[,]

" ============ Folding ============
" set foldmethod=indent
set foldlevel=1

" ============ Keymaps ============
" j/k move by display line, so wrapped lines behave as expected
nnoremap j gj
nnoremap k gk

" F2 toggles paste mode (avoids re-indenting pasted text)
set pastetoggle=<F2>

" Fix common :Q / :W / :WQ / :Wq typos
cabbrev Q q
cabbrev W w
cabbrev WQ wq
cabbrev Wq wq

" ============ Autocmds ============
" Restore cursor position when reopening a file
augroup vimrc_cursor_position
  autocmd!
  autocmd BufReadPost *
        \ if line("'\"") > 0 && line("'\"") <= line("$") |
        \   exe "normal! g`\"" |
        \ endif
augroup END

" ============ Input method ============
" Turn fcitx off on InsertLeave, restore it on InsertEnter

" FC4
augroup fcitx4_vim
  autocmd!
  if executable('fcitx-remote')
    let g:fcitx_state = 0

    autocmd InsertLeave *
          \ let g:fcitx_state = str2nr(system('fcitx-remote')) |
          \ call system('fcitx-remote -c')

    autocmd InsertEnter *
          \ if g:fcitx_state == 2 |
          \   call system('fcitx-remote -o') |
          \ endif
  endif
augroup END

" FC5
augroup fcitx5_vim
  autocmd!
  if executable('fcitx5-remote')
    let g:fcitx5_insert_state = 0

    autocmd InsertLeave *
          \ let g:fcitx5_insert_state = str2nr(system('fcitx5-remote')) |
          \ call system('fcitx5-remote -c')

    autocmd InsertEnter *
          \ if g:fcitx5_insert_state == 2 |
          \   call system('fcitx5-remote -o') |
          \ endif
  endif
augroup END

" ============ Plugins ============
if !empty(globpath(&runtimepath, 'autoload/plug.vim'))
call plug#begin('~/.vim/plugged')
" Plug 'morhetz/gruvbox'
Plug 'joshdick/onedark.vim'
Plug 'scrooloose/nerdtree'
Plug 'sheerun/vim-polyglot'
Plug 'jiangmiao/auto-pairs'
Plug 'tpope/vim-commentary'
Plug 'Yggdroot/indentLine'
Plug 'tpope/vim-fugitive'
Plug 'vim-airline/vim-airline'
Plug 'vim-airline/vim-airline-themes'
call plug#end()
endif

" ============ Theme ============
set background=dark

if has("termguicolors")
  set termguicolors
endif

" colorscheme gruvbox
" colorscheme pablo
if !empty(globpath(&runtimepath, 'colors/onedark.vim'))
  colorscheme onedark
else
  colorscheme default
endif

let g:airline_theme = 'onedark'
let g:airline_powerline_fonts = 1
